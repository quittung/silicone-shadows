"""Reviewer-only pending-submission routes."""

import shutil
from typing import Literal
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response

from ..artifacts import length_preview
from ..models import IndependentSubmission, IndependentUpdate, ReviewState
from ..workspace import Workspace

Rating = Literal["unusable", "bad_perspective", "good"]


def register(app: FastAPI, workspace: Workspace) -> None:
    store = workspace.hosted_store

    def require_reviewer(request: Request) -> None:
        user = request.state.user
        if not user or not user.reviewer:
            raise HTTPException(status_code=403, detail="reviewer access required")

    @app.get("/api/submissions/{item_id}/outline.svg")
    def pending_outline(
        item_id: str, request: Request, show_length: bool = False
    ) -> Response:
        require_reviewer(request)
        if not store or not store.submission(item_id):
            raise HTTPException(status_code=404, detail="pending submission not found")
        path = workspace.pending_paths(item_id)["svg"]
        if not path.is_file():
            raise HTTPException(
                status_code=404, detail="submission has no usable outline"
            )
        headers = {"Cache-Control": "no-store"}
        if show_length:
            return Response(
                length_preview(path), media_type="image/svg+xml", headers=headers
            )
        return FileResponse(path, media_type="image/svg+xml", headers=headers)

    @app.get("/api/moderation/submissions")
    def moderation_submissions(request: Request) -> dict:
        require_reviewer(request)
        submissions = []
        for row in store.submissions():
            independent = row["kind"] in {"independent", "independent_update"}
            if row["kind"] == "independent":
                state = IndependentSubmission.model_validate_json(row["state_json"])
            elif row["kind"] == "independent_update":
                state = IndependentUpdate.model_validate_json(row["state_json"])
            else:
                state = ReviewState.model_validate_json(row["state_json"])
            rating = state.metadata.quality if independent else state.rating
            products = (
                [
                    {
                        "id": None,
                        "name": state.metadata.name,
                        "vendor": state.metadata.vendor,
                        "type": state.metadata.product_type,
                    }
                ]
                if independent
                else [
                    {
                        "id": product["id"],
                        "name": product.get("n", ""),
                        "vendor": product.get("vn", ""),
                        "type": product.get("pt", ""),
                    }
                    for product in workspace.catalog_by_stem.get(row["item_id"], [])
                ]
            )
            submissions.append(
                {
                    "item_id": row["item_id"],
                    "contributor": row["user_name"],
                    "created_at": row["created_at"],
                    "kind": row["kind"],
                    "rating": rating,
                    "source": row["source"],
                    "products": products,
                    "outline_url": (
                        f"/api/submissions/{quote(row['item_id'], safe='')}/outline.svg?show_length=true"
                        if independent or rating != "unusable"
                        else None
                    ),
                    "source_url": (
                        None
                        if row["kind"] == "independent_update"
                        else f"/api/moderation/submissions/{quote(row['item_id'], safe='')}/source"
                    ),
                }
            )
        return {"submissions": submissions}

    @app.get("/api/moderation/submissions/{item_id}/source")
    def moderation_source(item_id: str, request: Request) -> FileResponse:
        require_reviewer(request)
        submission = store.submission(item_id)
        if not submission:
            raise HTTPException(status_code=404, detail="pending submission not found")
        alternative = workspace.pending_paths(item_id)["alternative"]
        path = (
            alternative if alternative.is_file() else workspace.download_source(item_id)
        )
        return FileResponse(path, headers={"Cache-Control": "no-store"})

    @app.post("/api/moderation/submissions/{item_id}/approve")
    def approve_submission(
        item_id: str, request: Request, rating: Rating | None = None
    ) -> Response:
        require_reviewer(request)
        submission = store.submission(item_id)
        if not submission:
            raise HTTPException(status_code=404, detail="pending submission not found")
        pending = workspace.pending_paths(item_id)
        if rating == "unusable" and submission["kind"] != "catalog":
            raise HTTPException(
                status_code=400,
                detail="independent products cannot be rated unusable",
            )
        if submission["kind"] == "independent":
            if not pending["svg"].is_file():
                raise HTTPException(
                    status_code=500, detail="pending outline is missing"
                )
            state = IndependentSubmission.model_validate_json(submission["state_json"])
            if rating:
                state.metadata.quality = rating
            workspace.publish_independent(item_id, state, pending["svg"])
        elif submission["kind"] == "independent_update":
            state = IndependentUpdate.model_validate_json(submission["state_json"])
            if rating:
                state.metadata.quality = rating
            try:
                workspace.update_independent(state.record_id, state.metadata)
            except ValueError as error:
                raise HTTPException(status_code=400, detail=str(error)) from error
        else:
            state = ReviewState.model_validate_json(submission["state_json"])
            if rating:
                state.rating = rating
            svg_path = pending["svg"] if state.rating != "unusable" else None
            if svg_path and not svg_path.is_file():
                raise HTTPException(
                    status_code=500, detail="pending outline is missing"
                )
            workspace.publish(item_id, state, svg_path, submission["source"])
        store.remove_submission(item_id)
        if pending["directory"].is_dir():
            shutil.rmtree(pending["directory"])
        return Response(status_code=204)

    @app.post("/api/moderation/submissions/{item_id}/reject")
    def reject_submission(item_id: str, request: Request) -> Response:
        require_reviewer(request)
        if not store.submission(item_id):
            raise HTTPException(status_code=404, detail="pending submission not found")
        store.remove_submission(item_id)
        directory = workspace.pending_paths(item_id)["directory"]
        if directory.is_dir():
            shutil.rmtree(directory)
        return Response(status_code=204)
