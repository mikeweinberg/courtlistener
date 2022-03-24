import logging
import random
import traceback
from typing import List, Optional, Tuple, Union

import requests
from django.apps import apps
from django.conf import settings
from django.core.files.base import ContentFile
from juriscraper.pacer import CaseQuery, PacerSession

from cl.audio.models import Audio
from cl.celery_init import app
from cl.citations.tasks import (
    find_citations_and_parentheticals_for_opinion_by_pks,
)
from cl.custom_filters.templatetags.text_filters import best_case_name
from cl.lib.celery_utils import throttle_task
from cl.lib.juriscraper_utils import get_scraper_object_by_name
from cl.lib.microservice_utils import microservice
from cl.lib.pacer import map_cl_to_pacer_id
from cl.lib.pacer_session import get_or_cache_pacer_cookies
from cl.lib.privacy_tools import anonymize, set_blocked_status
from cl.lib.string_utils import trunc
from cl.lib.utils import is_iter
from cl.recap.mergers import save_iquery_to_docket
from cl.search.models import Docket, Opinion, RECAPDocument

DEVNULL = open("/dev/null", "w")

logger = logging.getLogger(__name__)

ExtractProcessResult = Tuple[str, Optional[str]]


def update_document_from_text(opinion: Opinion) -> None:
    """Extract additional metadata from document text

    We use this code with BIA decisions. Previously Tax.
    I think it is not unlikely that we will use or need this in the future.

    Use functions from Juriscraper to pull metadata out of opinion
    text. Formerly implemented in only Tax Court, but functional in all
    scrapers via AbstractSite object.

    Note that this updates the values but does not save them. Saving is left to
    the calling function.

    :param opinion: Opinion object
    :return: None
    """
    court = opinion.cluster.docket.court.pk
    site = get_scraper_object_by_name(court)
    if site is None:
        return
    metadata_dict = site.extract_from_text(opinion.plain_text)
    for model_name, data in metadata_dict.items():
        ModelClass = apps.get_model(f"search.{model_name}")
        if model_name == "Docket":
            opinion.cluster.docket.__dict__.update(data)
        elif model_name == "OpinionCluster":
            opinion.cluster.__dict__.update(data)
        elif model_name == "Citation":
            data["cluster_id"] = opinion.cluster_id
            ModelClass.objects.get_or_create(**data)
        else:
            raise NotImplementedError(
                f"Object type of {model_name} not yet supported."
            )


@app.task
def extract_doc_content(
    pk: int,
    ocr_available: bool = False,
    citation_jitter: bool = False,
) -> None:
    """
    Given an opinion PK, we extract it, sniffing its extension, then store its
    contents in the database.  Finally, we asynchronously find citations in
    the document content and match them to other documents.

    This implementation uses local paths.

    :param pk: The opinion primary key to work on
    :param ocr_available: Whether the PDF converting function should use OCR
    :param citation_jitter: Whether to apply jitter before running the citation
    parsing code. This can be useful do spread these tasks out when doing a
    larger scrape.
    """
    opinion = Opinion.objects.get(pk=pk)
    response = microservice(
        service="document-extract",
        item=opinion,
        params={"ocr_available": ocr_available},
    )
    if not response.ok:
        logging.warning(
            f"Error from document-extract microservice: {response.status_code}"
        )
        return

    data = response.json()
    content = data["content"]
    extension = opinion.local_path.name.split(".")[-1]
    opinion.extracted_by_ocr = data["extracted_by_ocr"]

    if data["page_count"]:
        opinion.page_count = data["page_count"]

    assert isinstance(
        content, str
    ), f"content must be of type str, not {type(content)}"

    set_blocked_status(opinion, content, extension)
    update_document_from_text(opinion)

    if data["err"]:
        logger.error(
            f"****Error: {data['err']}, extracting text from {extension}: {opinion}****"
        )
        return

    # Save item, and index Solr if needed.
    # noinspection PyBroadException
    try:
        opinion.cluster.docket.save()
        opinion.cluster.save(index=False)
        if not citation_jitter:
            # No waiting around. Save to the database now, but don't bother
            # with the index yet because citations are being done imminently.
            opinion.save(index=False)
        else:
            # Save to the index now, citations come later, commit comes
            # according to schedule
            opinion.save(index=True)
    except Exception:
        print(
            "****Error saving text to the db for: %s****\n%s"
            % (opinion, traceback.format_exc())
        )
        return

    # Identify and link citations within the document content
    find_citations_and_parentheticals_for_opinion_by_pks.apply_async(
        ([opinion.pk],), countdown=random.randint(0, 3600)
    )


@app.task
def extract_recap_pdf(
    pks: Union[int, List[int]],
    skip_ocr: bool = False,
    check_if_needed: bool = True,
) -> List[int]:
    """Extract the contents from a RECAP PDF if necessary."""
    if not is_iter(pks):
        pks = [pks]

    processed = []
    for pk in pks:
        rd = RECAPDocument.objects.get(pk=pk)
        if check_if_needed and not rd.needs_extraction:
            # Early abort if the item doesn't need extraction and the user
            # hasn't disabled early abortion.
            processed.append(pk)
            continue

        response = microservice(
            service="document-extract",
            item=rd,
            params={"ocr_available": not skip_ocr},
        )
        if not response.ok:
            print("Error from microservice")
            continue

        content = response.json()["content"]
        has_content = bool(content)
        extracted_by_ocr = response.json()["extracted_by_ocr"]
        match has_content, extracted_by_ocr:
            case True, True:
                rd.ocr_status = RECAPDocument.OCR_COMPLETE
            case True, False:
                if skip_ocr:
                    rd.ocr_status = RECAPDocument.OCR_UNNECESSARY
            case False, True:
                rd.ocr_status = RECAPDocument.OCR_FAILED
            case False, False:
                rd.ocr_status = RECAPDocument.OCR_NEEDED    

        rd.plain_text, _ = anonymize(content)
        # Do not do indexing here. Creates race condition in celery.
        rd.save(index=False, do_extraction=False)
        processed.append(pk)

    return processed


@app.task(bind=True, max_retries=1, countdown=2)
def process_audio_file(self, pk) -> None:
    """Given the key to an audio file, extract its content and add the related
    meta data to the database.

    :param self: A Celery task object
    :param pk: Audio file pk
    :return: None
    """
    audio_obj = Audio.objects.get(pk=pk)
    date_argued = audio_obj.docket.date_argued
    if date_argued:
        date_argued_str = date_argued.strftime("%Y-%m-%d")
        date_argued_year = date_argued.year
    else:
        date_argued_str, date_argued_year = None, None

    audio_data = {
        "court_full_name": audio_obj.docket.court.full_name,
        "court_short_name": audio_obj.docket.court.short_name,
        "court_pk": audio_obj.docket.court.pk,
        "court_url": audio_obj.docket.court.url,
        "docket_number": audio_obj.docket.docket_number,
        "date_argued": date_argued_str,
        "date_argued_year": date_argued_year,
        "case_name": audio_obj.case_name,
        "case_name_full": audio_obj.case_name_full,
        "case_name_short": audio_obj.case_name_short,
        "download_url": audio_obj.download_url,
    }
    audio_response = microservice(
        service="convert-audio",
        item=audio_obj,
        params=audio_data,
    )
    audio_response.raise_for_status()
    cf = ContentFile(audio_response.content)
    file_name = f"{trunc(best_case_name(audio_obj).lower(), 72)}_cl.mp3"
    audio_obj.file_with_date = audio_obj.docket.date_argued
    audio_obj.local_path_mp3.save(file_name, cf, save=False)
    audio_obj.duration = float(
        microservice(
            service="audio-duration",
            file=audio_response.content,
            file_type="mp3",
        ).text
    )
    audio_obj.processing_complete = True
    audio_obj.save()


@app.task(bind=True, max_retries=2, interval_start=5, interval_step=5)
@throttle_task("1/s", key="court_id")
def update_docket_info_iquery(self, d_pk: int, court_id: str) -> None:
    """Update the docket info from iquery

    :param self: The Celery task
    :param d_pk: The ID of the docket
    :param court_id: The court of the docket. Needed for throttling by court.
    :return: None
    """
    cookies = get_or_cache_pacer_cookies(
        "pacer_scraper",
        settings.PACER_USERNAME,
        password=settings.PACER_PASSWORD,
    )
    s = PacerSession(
        cookies=cookies,
        username=settings.PACER_USERNAME,
        password=settings.PACER_PASSWORD,
    )
    d = Docket.objects.get(pk=d_pk, court_id=court_id)
    report = CaseQuery(map_cl_to_pacer_id(d.court_id), s)
    try:
        report.query(d.pacer_case_id)
    except (requests.Timeout, requests.RequestException) as exc:
        logger.warning(
            "Timeout or unknown RequestException on iquery crawl. "
            "Trying again if retries not exceeded."
        )
        if self.request.retries == self.max_retries:
            return
        raise self.retry(exc=exc)
    if not report.data:
        return

    save_iquery_to_docket(
        self,
        report.data,
        d,
        tag_names=None,
        add_to_solr=True,
    )
