"""
Upload-Post API integration for cross-posting videos to TikTok, Instagram and YouTube Shorts.

Docs: https://docs.upload-post.com
"""
import os
from typing import Optional

import requests
from loguru import logger
from app.config import config


class UploadPostService:
    API_BASE = "https://api.upload-post.com"

    @property
    def api_key(self) -> str:
        return config.app.get("upload_post_api_key", "")

    @property
    def username(self) -> str:
        return config.app.get("upload_post_username", "")

    @property
    def enabled(self) -> bool:
        return config.app.get("upload_post_enabled", False)

    @property
    def platforms(self) -> list:
        return config.app.get("upload_post_platforms", ["tiktok", "instagram"])

    @property
    def auto_upload(self) -> bool:
        return config.app.get("upload_post_auto_upload", False)

    @property
    def youtube_privacy_status(self) -> str:
        return config.app.get("upload_post_youtube_privacy_status", "public")

    def is_configured(self) -> bool:
        return bool(self.api_key and self.username and self.enabled)

    def upload_video(
        self,
        video_path: str,
        title: str,
        platforms: Optional[list] = None,
        privacy_level: str = "PUBLIC_TO_EVERYONE",
        youtube_extra: Optional[dict] = None,
        scheduled_date: Optional[str] = None,
    ) -> dict:
        """
        Publish a video, or queue it for a future time.

        ``scheduled_date`` is ISO-8601 (e.g. ``2026-09-10T18:00:00Z``) and may
        be up to 365 days ahead. Upload-Post answers a scheduled request with
        202 and a ``job_id`` instead of publishing straight away; the caller
        should keep that id to check status or move the slot later.
        """
        if not self.is_configured():
            logger.warning("Upload-Post is not configured. Skipping cross-post.")
            return {"success": False, "error": "Upload-Post not configured"}

        if platforms is None:
            platforms = self.platforms

        if not os.path.exists(video_path):
            logger.error(f"Video file not found: {video_path}")
            return {"success": False, "error": f"Video file not found: {video_path}"}

        logger.info(f"Cross-posting video to {', '.join(platforms)} via Upload-Post...")

        try:
            with open(video_path, 'rb') as video_file:
                files = {'video': video_file}

                data = [
                    ('user', self.username),
                    ('title', title[:2200]),
                    ('privacy_level', privacy_level),
                ]

                if scheduled_date:
                    data.append(('scheduled_date', scheduled_date))

                for platform in platforms:
                    data.append(('platform[]', platform))

                if youtube_extra and any(p.startswith("youtube") for p in platforms):
                    if "youtube_title" in youtube_extra:
                        data.append(('youtube_title', youtube_extra["youtube_title"][:100]))
                    if "youtube_description" in youtube_extra:
                        data.append(('youtube_description', youtube_extra["youtube_description"]))
                    for tag in youtube_extra.get("tags", []):
                        data.append(('tags[]', tag))
                    data.append(('privacyStatus', youtube_extra.get("privacyStatus", "public")))
                    data.append(('containsSyntheticMedia', "true"))

                headers = {'Authorization': f'Apikey {self.api_key}'}

                response = requests.post(
                    f"{self.API_BASE}/api/upload",
                    headers=headers,
                    data=data,
                    files=files,
                    timeout=300,
                )

                response.raise_for_status()
                result = response.json()

                job_id = result.get('job_id')
                if scheduled_date and job_id:
                    # A scheduled request returns 202 with a job id and no
                    # success flag — it has not published yet, so treat the
                    # accepted job as the success signal.
                    logger.info(
                        f"🗓️ Video scheduled for {scheduled_date}. Job ID: {job_id}"
                    )
                    result.setdefault('success', True)
                elif result.get('success'):
                    logger.info(f"✅ Video cross-posted successfully! Request ID: {result.get('request_id')}")
                else:
                    logger.warning(f"Cross-post failed: {result.get('message', 'Unknown error')}")

                return result

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to cross-post video: {str(e)}")
            return {"success": False, "error": str(e)}

    def check_status(
        self, request_id: Optional[str] = None, job_id: Optional[str] = None
    ) -> dict:
        """
        Check the status of an upload request or a scheduled job.

        Args:
            request_id (str): The request ID from an immediate upload
            job_id (str): The job ID returned when scheduled_date was used

        Returns:
            dict: Status information
        """
        if not request_id and not job_id:
            return {"success": False, "error": "request_id or job_id is required"}

        try:
            headers = {
                'Authorization': f'Apikey {self.api_key}'
            }

            response = requests.get(
                f"{self.API_BASE}/api/uploadposts/status",
                params={'job_id': job_id} if job_id else {'request_id': request_id},
                headers=headers,
                timeout=30
            )

            response.raise_for_status()
            return response.json()

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to check status: {str(e)}")
            return {"success": False, "error": str(e)}


# Singleton instance
upload_post_service = UploadPostService()


def cross_post_video(
    video_path: str,
    title: str,
    platforms: Optional[list] = None,
    youtube_extra: Optional[dict] = None,
    scheduled_date: Optional[str] = None,
) -> dict:
    return upload_post_service.upload_video(
        video_path,
        title,
        platforms,
        youtube_extra=youtube_extra,
        scheduled_date=scheduled_date,
    )
