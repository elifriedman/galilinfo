from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse


PLACES_FILE = Path(__file__).resolve().parent.parent / "data" / "places.json"

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=[],
)


@app.get("/data/places.json", include_in_schema=False)
def get_places() -> FileResponse:
    return FileResponse(
        PLACES_FILE,
        media_type="application/json",
        headers={"Cache-Control": "public, max-age=60, must-revalidate"},
    )
