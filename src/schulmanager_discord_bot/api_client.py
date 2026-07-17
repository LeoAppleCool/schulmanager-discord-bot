from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx


class ApiClientError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(slots=True)
class LoginResponse:
    access_token: str
    refresh_token: str
    expires_in: int
    refresh_expires_in: int
    account_id: str
    student_ids: list[str]


class SchulmanagerApiClient:
    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=30)

    async def close(self) -> None:
        await self._client.aclose()

    async def login(self, email: str, password: str) -> LoginResponse:
        payload = await self._request_json(
            "POST",
            "/auth/login",
            json={"email": email, "password": password},
        )

        session = payload.get("session") or {}
        return LoginResponse(
            access_token=str(payload.get("access_token") or ""),
            refresh_token=str(payload.get("refresh_token") or ""),
            expires_in=int(payload.get("expires_in") or 0),
            refresh_expires_in=int(payload.get("refresh_expires_in") or 0),
            account_id=str(session.get("account_id") or ""),
            student_ids=[str(value) for value in (session.get("student_ids") or [])],
        )

    async def refresh(self, refresh_token: str) -> LoginResponse:
        payload = await self._request_json(
            "POST",
            "/auth/refresh",
            json={"refresh_token": refresh_token},
        )
        session = payload.get("session") or {}
        return LoginResponse(
            access_token=str(payload.get("access_token") or ""),
            refresh_token=str(payload.get("refresh_token") or ""),
            expires_in=int(payload.get("expires_in") or 0),
            refresh_expires_in=int(payload.get("refresh_expires_in") or 0),
            account_id=str(session.get("account_id") or ""),
            student_ids=[str(value) for value in (session.get("student_ids") or [])],
        )

    async def get_students(self, access_token: str) -> list[dict[str, Any]]:
        payload = await self._request_json("GET", "/students", access_token=access_token)
        return payload if isinstance(payload, list) else []

    async def get_schedule(
        self,
        access_token: str,
        student_id: str,
        from_date: date,
        to_date: date,
        force_refresh: bool = True,
    ) -> list[dict[str, Any]]:
        payload = await self._request_json(
            "GET",
            f"/students/{student_id}/schedule",
            access_token=access_token,
            params={
                "from_date": from_date.isoformat(),
                "to_date": to_date.isoformat(),
                "force_refresh": str(force_refresh).lower(),
            },
        )
        return payload if isinstance(payload, list) else []

    async def get_homework(
        self,
        access_token: str,
        student_id: str,
        *,
        open_only: bool = False,
        force_refresh: bool = True,
    ) -> list[dict[str, Any]]:
        payload = await self._request_json(
            "GET",
            f"/students/{student_id}/homework",
            access_token=access_token,
            params={
                "open_only": str(open_only).lower(),
                "force_refresh": str(force_refresh).lower(),
            },
        )
        return payload if isinstance(payload, list) else []

    async def get_grades(
        self,
        access_token: str,
        student_id: str,
        *,
        force_refresh: bool = True,
    ) -> list[dict[str, Any]]:
        payload = await self._request_json(
            "GET",
            f"/students/{student_id}/grades",
            access_token=access_token,
            params={"force_refresh": str(force_refresh).lower()},
        )
        return payload if isinstance(payload, list) else []

    async def get_grade_stats(
        self,
        access_token: str,
        student_id: str,
        *,
        force_refresh: bool = True,
    ) -> dict[str, Any]:
        payload = await self._request_json(
            "GET",
            f"/students/{student_id}/grades/stats",
            access_token=access_token,
            params={"force_refresh": str(force_refresh).lower()},
        )
        return payload if isinstance(payload, dict) else {}

    async def get_events(
        self,
        access_token: str,
        student_id: str,
        *,
        force_refresh: bool = True,
    ) -> list[dict[str, Any]]:
        payload = await self._request_json(
            "GET",
            f"/students/{student_id}/events",
            access_token=access_token,
            params={"force_refresh": str(force_refresh).lower()},
        )
        return payload if isinstance(payload, list) else []

    async def get_absences(
        self,
        access_token: str,
        student_id: str,
        *,
        force_refresh: bool = True,
    ) -> list[dict[str, Any]]:
        payload = await self._request_json(
            "GET",
            f"/students/{student_id}/absences",
            access_token=access_token,
            params={"force_refresh": str(force_refresh).lower()},
        )
        return payload if isinstance(payload, list) else []

    async def get_messages(
        self,
        access_token: str,
        student_id: str,
        *,
        force_refresh: bool = True,
    ) -> list[dict[str, Any]]:
        payload = await self._request_json(
            "GET",
            f"/students/{student_id}/messages",
            access_token=access_token,
            params={"force_refresh": str(force_refresh).lower()},
        )
        return payload if isinstance(payload, list) else []

    async def get_exams(
        self,
        access_token: str,
        student_id: str,
        *,
        force_refresh: bool = True,
    ) -> list[dict[str, Any]]:
        payload = await self._request_json(
            "GET",
            f"/students/{student_id}/exams",
            access_token=access_token,
            params={"force_refresh": str(force_refresh).lower()},
        )
        return payload if isinstance(payload, list) else []

    async def get_letters(
        self,
        access_token: str,
        student_id: str,
        *,
        force_refresh: bool = True,
    ) -> list[dict[str, Any]]:
        payload = await self._request_json(
            "GET",
            f"/students/{student_id}/letters",
            access_token=access_token,
            params={"force_refresh": str(force_refresh).lower()},
        )
        return payload if isinstance(payload, list) else []

    async def get_message_thread(
        self,
        access_token: str,
        student_id: str,
        subscription_id: str,
        *,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        payload = await self._request_json(
            "GET",
            f"/students/{student_id}/messages/{subscription_id}",
            access_token=access_token,
            params={"force_refresh": str(force_refresh).lower()},
        )
        return payload if isinstance(payload, dict) else {}

    async def get_calendar_ics(
        self,
        access_token: str,
        student_id: str,
    ) -> bytes:
        headers: dict[str, str] = {"Authorization": f"Bearer {access_token}"}
        response = await self._client.get(
            f"/students/{student_id}/calendar.ics",
            headers=headers,
        )
        if response.status_code >= 400:
            detail = self._extract_error_detail(response)
            raise ApiClientError(detail, status_code=response.status_code)
        return response.content

    async def patch_homework_done(
        self,
        access_token: str,
        student_id: str,
        homework_id: str,
        done: bool,
    ) -> dict[str, Any]:
        payload = await self._request_json(
            "PATCH",
            f"/students/{student_id}/homework/{homework_id}",
            access_token=access_token,
            json={"done": done},
        )
        return payload if isinstance(payload, dict) else {}

    async def flush_cache(self, access_token: str) -> None:
        headers: dict[str, str] = {"Authorization": f"Bearer {access_token}"}
        response = await self._client.delete("/cache", headers=headers)
        if response.status_code >= 400 and response.status_code != 204:
            detail = self._extract_error_detail(response)
            raise ApiClientError(detail, status_code=response.status_code)

    async def logout(self, access_token: str) -> None:
        await self._request_json(
            "POST",
            "/auth/logout",
            access_token=access_token,
        )

    async def get_me(self, access_token: str) -> dict[str, Any]:
        payload = await self._request_json("GET", "/auth/me", access_token=access_token)
        return payload if isinstance(payload, dict) else {}

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        access_token: str | None = None,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        headers: dict[str, str] = {}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"

        response = await self._client.request(
            method,
            path,
            headers=headers,
            json=json,
            params=params,
        )

        if response.status_code >= 400:
            detail = self._extract_error_detail(response)
            raise ApiClientError(detail, status_code=response.status_code)

        try:
            return response.json()
        except ValueError as exc:
            raise ApiClientError("API returned invalid JSON") from exc

    @staticmethod
    def _extract_error_detail(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return f"HTTP {response.status_code}"

        if isinstance(payload, dict):
            detail = payload.get("detail")
            if detail:
                return str(detail)
        return f"HTTP {response.status_code}"
