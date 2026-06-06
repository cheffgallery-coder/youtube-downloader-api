import asyncio
import json
import time
import traceback
import typing as t
from functools import lru_cache
from os import path
from pathlib import Path

from fastapi import (
    APIRouter,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    status,
)
from httpx import Proxy
from innertube import InnerTube
from pydantic import ValidationError
from starlette.websockets import WebSocketState
from yt_dlp import YoutubeDL
from yt_dlp_bonus import Downloader, YoutubeDLBonus
from yt_dlp_bonus.constants import audioQualities, videoQualities
from yt_dlp_bonus.utils import get_size_string

import app.v1.models as models
from app.config import DOWNLOAD_DIR, TEMP_DIR, loaded_config
from app.models import CustomWebsocketResponse
from app.utils import (
    get_absolute_link_to_static_file,
    logger,
    router_exception_handler,
    sanitize_filename,
    silence_websocket_exceptions,
)
from app.v1.download_manager import download_manager  # ← only new import
from app.v1.utils import get_extracted_info

router = APIRouter(prefix="/v1")

yt_params = loaded_config.ytdlp_params

yt_params.update({
    "paths": {"home": DOWNLOAD_DIR.as_posix(), "temp": TEMP_DIR.name}
})

yt = YoutubeDLBonus(params=yt_params)

downloader = Downloader(
    yt=yt,
    working_directory=DOWNLOAD_DIR,
    clear_temps=loaded_config.clear_temps,
    filename_prefix=loaded_config.filename_prefix,
)


PARAMS_TYPE_VIDEO = "EgIQAQ%3D%3D"

innertube_client = InnerTube(
    "WEB",
    "2.20230920.00.00",
    # proxies=None if not loaded_config.proxy else Proxy(loaded_config.proxy),
)


@lru_cache(maxsize=100)
def search_videos_by_key(query: str, limit: int = -1) -> list[dict[str, str]]:
    """Perform a video search.

    Args:
        query (str): Search keyword
        limit (int): Total results not to exceed. Defaults to -1 (No limit).

    Returns:
        list[dict[str, str]]: Sorted shallow results.
    """
    video_search_results = innertube_client.search(
        query, params=PARAMS_TYPE_VIDEO
    )
    video_metadata_container: list[dict] = []
    contents = video_search_results["contents"]["twoColumnSearchResultsRenderer"][
        "primaryContents"
    ]["sectionListRenderer"]["contents"][0]["itemSectionRenderer"]["contents"]
    count = 0
    for content in contents:
        try:
            video = content["videoRenderer"]
            video_id = video["videoId"]
            video_title = video["title"]["runs"][0]["text"]
            video_duration = video["lengthText"]["simpleText"]
            video_metadata_container.append(
                dict(id=video_id, title=video_title, duration=video_duration)
            )
            count += 1
            if count == limit:
                break

        except Exception:  # KeyError etc
            pass
    return video_metadata_container


@router.get("/search", name="Search videos")
@router_exception_handler
def search_videos(
    q: str = Query(description="Video title or keyword"),
    limit: int = Query(
        10,
        gt=0,
        le=loaded_config.search_limit,
        description="Videos amount not to exceed.",
    ),
) -> models.SearchVideosResponse:
    """Search videos
    - Search videos matching the query and return whole results at once.
    - Serves from cache similar `99` subsequent queries.
    """
    videos_found = search_videos_by_key(query=q, limit=limit)
    if not videos_found:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No video matched that query - {q}!",
        )
    return models.SearchVideosResponse(query=q, results=videos_found)


@router.get("/metadata", name="Video metadata")
@router_exception_handler
def get_video_metadata(
    url: str = Query(description="Video URL or ID"),
) -> models.VideoMetadataResponse:
    """Get metadata of a specific video.
    - Similar subsequent requests will be faster as they will be served
    from the cache for a few hours.
    """
    extracted_info = get_extracted_info(yt=yt, url=url)
    video_formats = yt.get_video_qualities_with_extension(
        extracted_info,
        ext=loaded_config.default_extension,
        audio_ext=loaded_config.default_audio_format,
    )
    updated_video_formats = yt.update_audio_video_size(video_formats)
    audio_formats = []
    video_formats = []
    for quality, format in updated_video_formats.items():
        if quality in audioQualities:
            audio_formats.append(
                dict(
                    quality=quality,
                    size=get_size_string(format.audio_video_size),
                )
            )
        else:
            video_formats.append(
                dict(
                    quality=quality,
                    size=get_size_string(format.audio_video_size),
                )
            )

    return models.VideoMetadataResponse(
        id=extracted_info.id,
        title=extracted_info.title,
        channel=extracted_info.channel,
        uploader_url=extracted_info.uploader_url,
        duration_string=extracted_info.duration_string,
        thumbnail=extracted_info.thumbnail,
        audio=audio_formats or [{"quality": "bestaudio"}],
        video=video_formats or [{"quality": "best"}],
        format=dict(
            audio=loaded_config.default_audio_format,
            video="mp4",
        ),
        others=dict(
            like_count=extracted_info.like_count,
            views_count=extracted_info.view_count,
            categories=extracted_info.categories or [],
            tags=extracted_info.tags or [],
        ),
    )


@router.post("/download", name="Process download")
def process_video_for_download(
    request: Request,
    payload: models.MediaDownloadProcessPayload,
    x_lang: t.Annotated[
        str,
        Header(
            description="Two-letter ISO set language code for subtitle purposes."
        ),
    ] = None,
) -> models.MediaDownloadResponse:
    """Initiate download processing
    - To download the media file: Add parameter `download` with value
    `true` to the returned link i.e `?download=true`.
    - Accomplish the same using websocket endpoint at `/api/v1/download/ws`
    """
    payload.x_lang = x_lang or payload.x_lang
    return real_download_process(request, payload)


def _quality_to_height(quality: str) -> int | None:
    """Convert qualities like '360p' or '720p60' to a max height integer."""
    if not quality:
        return None
    digits = "".join(ch for ch in quality if ch.isdigit())
    if not digits:
        return None
    # 720p60 becomes 72060 with the simple join above, so handle p split first.
    if "p" in quality:
        digits = quality.split("p", 1)[0]
    try:
        return int(digits)
    except ValueError:
        return None


def _snapshot_download_dir() -> dict[str, float]:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    snapshot: dict[str, float] = {}
    for file in DOWNLOAD_DIR.rglob("*"):
        if file.is_file():
            try:
                snapshot[str(file)] = file.stat().st_mtime
            except OSError:
                pass
    return snapshot


def _pick_downloaded_file(info: dict, before: dict[str, float]) -> Path:
    """Find the file created/updated by yt-dlp as robustly as possible."""
    possible_paths: list[Path] = []

    # yt-dlp often returns final file paths here.
    for item in info.get("requested_downloads") or []:
        filepath = item.get("filepath") or item.get("filename")
        if filepath:
            possible_paths.append(Path(filepath))

    # Sometimes _filename is present.
    if info.get("_filename"):
        possible_paths.append(Path(info["_filename"]))

    for candidate in possible_paths:
        if candidate.exists() and candidate.is_file():
            return candidate

    # Fallback: latest changed file in DOWNLOAD_DIR.
    changed: list[Path] = []
    for file in DOWNLOAD_DIR.rglob("*"):
        if not file.is_file():
            continue
        if file.name.endswith(".part") or file.name.endswith(".ytdl"):
            continue
        try:
            old_mtime = before.get(str(file))
            new_mtime = file.stat().st_mtime
            if old_mtime is None or new_mtime > old_mtime + 0.001:
                changed.append(file)
        except OSError:
            pass

    if changed:
        return max(changed, key=lambda f: f.stat().st_mtime)

    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Download finished but output file could not be found in static directory.",
    )


def _build_direct_ytdlp_opts(
    payload: models.MediaDownloadProcessPayload,
    progress_hooks: list[t.Callable],
    extracted_info,
) -> dict:
    """Use direct yt-dlp selectors instead of yt_dlp_bonus quality map.

    This avoids the metadata/download mismatch where metadata shows 360p/720p
    but the download quality map rejects the same quality.
    """
    id_placeholder = " [%(id)s]" if loaded_config.append_id_in_filename else ""
    prefix = loaded_config.filename_prefix or ""
    safe_title = sanitize_filename(extracted_info.title or "youtube-video")

    # Keep output inside configured static download directory.
    outtmpl = f"{prefix}{safe_title}{id_placeholder} %(format_note)s.%(ext)s"

    opts = dict(yt_params)
    opts.update({
        "noplaylist": True,
        "paths": {"home": DOWNLOAD_DIR.as_posix(), "temp": TEMP_DIR.name},
        "outtmpl": outtmpl,
        "retries": loaded_config.retries,
        "continuedl": loaded_config.continuedl,
        "nopart": loaded_config.nopart,
        "noprogress": loaded_config.noprogress,
        "quiet": loaded_config.quiet,
        "verbose": loaded_config.verbose,
        "progress_hooks": progress_hooks or [],
        "http_chunk_size": loaded_config.http_chunk_size,
        "concurrent_fragment_downloads": loaded_config.concurrent_fragment_downloads,
    })

    if loaded_config.proxy:
        opts["proxy"] = loaded_config.proxy
    if loaded_config.cookiefile:
        opts["cookiefile"] = loaded_config.cookiefile

    # Audio qualities: ultralow/low/medium OR bestaudio.
    if payload.quality in audioQualities or payload.quality == "bestaudio":
        audio_ext = loaded_config.default_audio_format or "m4a"
        opts["format"] = f"bestaudio[ext={audio_ext}]/bestaudio/best"
        opts["outtmpl"] = f"{prefix}{safe_title}{id_placeholder} audio.%(ext)s"
        if payload.bitrate:
            # Convert to mp3 only when bitrate is requested by caller.
            opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": str(payload.bitrate).replace("k", ""),
            }]
        return opts

    # Video qualities: 360p, 720p, 1080p, etc.
    if payload.quality in videoQualities:
        height = _quality_to_height(payload.quality)
        if height:
            opts["format"] = (
                f"bestvideo[ext=mp4][height<={height}]+bestaudio[ext=m4a]/"
                f"bestvideo[height<={height}]+bestaudio/"
                f"best[ext=mp4][height<={height}]/"
                f"best[height<={height}]/best"
            )
        else:
            opts["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
        opts["merge_output_format"] = "mp4"
        return opts

    # best / bestvideo fallback.
    if payload.quality in {"best", "bestvideo"}:
        opts["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
        opts["merge_output_format"] = "mp4"
        return opts

    opts["format"] = payload.quality or "best"
    opts["merge_output_format"] = "mp4"
    return opts


def real_download_process(
    request: Request | WebSocket,
    payload: models.MediaDownloadProcessPayload,
    progress_hooks: list[t.Callable] = [],
    **kwargs,
) -> models.MediaDownloadResponse:
    extracted_info = get_extracted_info(yt=yt, url=payload.url)

    if loaded_config.embed_subtitles and payload.x_lang is not None:
        # Leave subtitle embedding off during provider testing unless needed.
        logger.warning("Subtitle embedding was requested but direct test downloader ignores subtitles for stability.")

    before = _snapshot_download_dir()
    opts = _build_direct_ytdlp_opts(payload, progress_hooks, extracted_info)

    try:
        logger.info(
            "direct_ytdlp_download_start video_id=%s quality=%s format=%s",
            extracted_info.id,
            payload.quality,
            opts.get("format"),
        )
        started = time.time()
        with YoutubeDL(opts) as ydl:
            processed_info_dict = ydl.extract_info(payload.url, download=True)
        filepath = _pick_downloaded_file(processed_info_dict, before)
        logger.info(
            "direct_ytdlp_download_done video_id=%s quality=%s file=%s elapsed=%.2fs",
            extracted_info.id,
            payload.quality,
            filepath.name,
            time.time() - started,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "direct_ytdlp_download_failed video_id=%s quality=%s error=%s\n%s",
            getattr(extracted_info, "id", None),
            payload.quality,
            str(exc),
            traceback.format_exc(),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"yt-dlp download failed: {str(exc)[:500]}",
        )

    return models.MediaDownloadResponse(
        is_success=True,
        filename=filepath.name,
        filesize=get_size_string(path.getsize(filepath)),
        link=get_absolute_link_to_static_file(filepath.name, request),
    )


# ── Only this handler changed ────────────────────────────────────────────────


@router.websocket("/download/ws", name="Process download (websocket)")
async def download_websocket_handler(websocket: WebSocket):
    await websocket.accept()

    try:
        payload_dict: dict = await websocket.receive_json()
        payload = models.MediaDownloadProcessPayload(**payload_dict)

        def run_download(progress_hooks: list[t.Callable]):
            return real_download_process(
                request=websocket,
                payload=payload,
                progress_hooks=progress_hooks,
            )

        queue = await download_manager.subscribe(
            url=payload.url,
            quality=payload.quality,
            run_download_fn=run_download,
        )

        while True:
            message = await queue.get()
            await websocket.send_json(message)
            if message["status"] in ("completed", "error"):
                break

    except ValidationError as e:
        error = CustomWebsocketResponse(
            status="error", detail=dict(errors=json.loads(e.json()))
        )
        await websocket.send_json(error.model_dump())

    except Exception as e:
        logger.error(f"Websocket error {e}")

    finally:
        if websocket.client_state == WebSocketState.CONNECTED:
            await websocket.close()
