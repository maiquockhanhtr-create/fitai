import os
import io
import time
import base64
import asyncio
import logging
from collections import defaultdict, deque
from pathlib import Path

import httpx
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from PIL import Image, ImageOps

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fitai")

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

DEFAULT_FASHN_KEY = os.getenv("FASHN_API_KEY", "").strip()
DEFAULT_HF_TOKEN = os.getenv("HF_TOKEN", "").strip()
MODEL_NAME = os.getenv("FASHN_MODEL", "tryon-v1.6").strip()
ALLOWED_ORIGINS_RAW = os.getenv("ALLOWED_ORIGINS", "*").strip()
RATE_LIMIT_PER_WINDOW = int(os.getenv("RATE_LIMIT_PER_WINDOW", "8"))
RATE_LIMIT_WINDOW_SEC = int(os.getenv("RATE_LIMIT_WINDOW_SEC", "600"))
MAX_BYTES = 12 * 1024 * 1024
MAX_DIMENSION = 1536

app = FastAPI(title="FITAI Virtual Try-On AI", version="2.1.0")

allowed_origins = (
    ["*"]
    if ALLOWED_ORIGINS_RAW == "*" or not ALLOWED_ORIGINS_RAW
    else [o.strip() for o in ALLOWED_ORIGINS_RAW.split(",") if o.strip()]
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)

static_dir = ROOT / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")

# Lưu vết số lượt gọi theo IP trong bộ nhớ (không lưu ảnh người dùng)
_ip_requests: dict[str, deque[float]] = defaultdict(deque)
_rate_lock = asyncio.Lock()


def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "").strip()
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def enforce_rate_limit(request: Request) -> None:
    ip = get_client_ip(request)
    now = time.monotonic()
    async with _rate_lock:
        q = _ip_requests[ip]
        while q and (now - q[0]) > RATE_LIMIT_WINDOW_SEC:
            q.popleft()
        if len(q) >= RATE_LIMIT_PER_WINDOW:
            minutes = max(1, RATE_LIMIT_WINDOW_SEC // 60)
            raise HTTPException(
                429,
                f"Bạn đã thử đồ tối đa {RATE_LIMIT_PER_WINDOW} lần trong {minutes} phút. Vui lòng thử lại sau."
            )
        q.append(now)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


@app.get("/", response_class=HTMLResponse)
async def home():
    index_file = static_dir / "index.html"
    if not index_file.exists():
        raise HTTPException(500, "Không tìm thấy giao diện index.html.")
    return index_file.read_text(encoding="utf-8")


def clean_image_bytes(raw: bytes) -> tuple[bytes, str]:
    try:
        im = Image.open(io.BytesIO(raw))
        im.verify()
        im = Image.open(io.BytesIO(raw))
        im = ImageOps.exif_transpose(im)
    except Exception:
        raise HTTPException(400, "Tệp tải lên không phải ảnh hợp lệ hoặc bị hỏng.")

    if im.mode != "RGB":
        im = im.convert("RGB")

    if max(im.size) > MAX_DIMENSION:
        im.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.Resampling.LANCZOS)

    out_buf = io.BytesIO()
    im.save(out_buf, format="JPEG", quality=92, optimize=True)
    return out_buf.getvalue(), "image/jpeg"


async def read_image(file: UploadFile, label: str) -> tuple[bytes, str]:
    raw = await file.read()
    if not raw:
        raise HTTPException(400, f"Ảnh {label} đang trống.")
    if len(raw) > MAX_BYTES:
        raise HTTPException(413, f"Ảnh {label} vượt quá 12 MB. Hãy nén ảnh rồi thử lại.")
    return clean_image_bytes(raw)


def is_valid_key(key: str) -> bool:
    return bool(key) and key not in ("dan_api_key_cua_ban_vao_day", "YOUR_REAL_API_KEY", "your_token_here")


async def run_fashn_ai(
    api_key: str,
    person_bytes: bytes,
    garment_bytes: bytes,
    category: str,
    mode: str,
    prompt: str,
) -> str:
    person_uri = "data:image/jpeg;base64," + base64.b64encode(person_bytes).decode("ascii")
    garment_uri = "data:image/jpeg;base64," + base64.b64encode(garment_bytes).decode("ascii")

    if MODEL_NAME == "tryon-max":
        inputs = {
            "model_image": person_uri,
            "product_image": garment_uri,
            "generation_mode": mode,
            "resolution": "1k",
            "output_format": "jpeg",
            "return_base64": True,
        }
        if prompt.strip():
            inputs["prompt"] = prompt.strip()[:300]
    else:
        inputs = {
            "model_image": person_uri,
            "garment_image": garment_uri,
            "category": category,
            "mode": mode,
            "output_format": "jpeg",
            "return_base64": True,
        }

    async with httpx.AsyncClient(timeout=90.0) as client:
        try:
            resp = await client.post(
                "https://api.fashn.ai/v1/run",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={"model_name": MODEL_NAME, "inputs": inputs},
            )
        except httpx.RequestError as exc:
            logger.error("FASHN network error: %s", exc)
            raise HTTPException(502, "Không kết nối được máy chủ AI.")

        if resp.status_code >= 400:
            logger.warning("FASHN provider status %s", resp.status_code)
            raise HTTPException(502, "Nhà cung cấp AI đang gặp sự cố hoặc hết hạn mức. Vui lòng thử lại sau.")

        prediction_id = resp.json().get("id")
        if not prediction_id:
            raise HTTPException(502, "Dịch vụ AI không trả về mã xử lý hợp lệ.")

        for _ in range(60):
            await asyncio.sleep(2.0)
            try:
                status_resp = await client.get(
                    f"https://api.fashn.ai/v1/status/{prediction_id}",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            except httpx.RequestError:
                continue

            if status_resp.status_code >= 400:
                continue

            status_data = status_resp.json()
            status = status_data.get("status")

            if status == "completed":
                output = status_data.get("output") or []
                if not output:
                    raise HTTPException(502, "AI hoàn tất nhưng không có ảnh kết quả.")
                img_result = output[0]
                if isinstance(img_result, str) and img_result.startswith("http"):
                    img_dl = await client.get(img_result)
                    if img_dl.status_code == 200:
                        b64 = base64.b64encode(img_dl.content).decode("ascii")
                        img_result = f"data:image/jpeg;base64,{b64}"
                return img_result

            if status in {"failed", "error"}:
                raise HTTPException(502, "AI không tạo được ảnh với cặp ảnh này. Hãy thử ảnh rõ nét hơn.")

        raise HTTPException(504, "AI xử lý quá lâu. Vui lòng thử lại.")


async def run_free_ai_space(
    person_bytes: bytes,
    garment_bytes: bytes,
    category: str,
    prompt: str,
    hf_token: str = "",
) -> str:
    try:
        from gradio_client import Client, handle_file
        import tempfile
    except ImportError:
        raise HTTPException(500, "Máy chủ chưa cài đặt đầy đủ thư viện xử lý AI.")

    def _infer() -> str:
        with tempfile.TemporaryDirectory() as tmpdir:
            p_path = os.path.join(tmpdir, "person.jpg")
            g_path = os.path.join(tmpdir, "garment.jpg")
            with open(p_path, "wb") as fp:
                fp.write(person_bytes)
            with open(g_path, "wb") as fg:
                fg.write(garment_bytes)

            desc = prompt.strip() or "stylish fashionable outfit, high quality, natural fit"
            spaces = [
                ("yisol/IDM-VTON", "idm"),
                ("zhengchong/CatVTON", "cat"),
            ]

            for space_id, kind in spaces:
                try:
                    kwargs = {"hf_token": hf_token} if is_valid_key(hf_token) else {}
                    client = Client(space_id, **kwargs)
                    if kind == "idm":
                        res = client.predict(
                            dict={"background": handle_file(p_path), "layers": [], "composite": None},
                            garm_img=handle_file(g_path),
                            garment_des=desc,
                            is_checked=True,
                            is_checked_crop=False,
                            denoise_steps=30,
                            seed=42,
                            api_name="/tryon",
                        )
                        out_path = res[0] if isinstance(res, (list, tuple)) else res
                    else:
                        cloth_type = "overall" if category == "one-pieces" else ("lower" if category == "bottoms" else "upper")
                        res = client.predict(
                            person_image={"background": handle_file(p_path), "layers": [], "composite": None},
                            cloth_image=handle_file(g_path),
                            cloth_type=cloth_type,
                            num_inference_steps=35,
                            guidance_scale=2.5,
                            seed=42,
                            show_type="result only",
                            api_name="/submit_function",
                        )
                        out_path = res[0] if isinstance(res, (list, tuple)) else res

                    if out_path and os.path.exists(out_path):
                        with open(out_path, "rb") as f_out:
                            raw_out = f_out.read()
                        return "data:image/jpeg;base64," + base64.b64encode(raw_out).decode("ascii")
                except Exception as e:
                    logger.warning("Space %s failed: %s", space_id, e)
                    continue

            raise RuntimeError("busy")

    try:
        return await asyncio.to_thread(_infer)
    except Exception:
        raise HTTPException(
            503,
            "Hệ thống AI đang bận hoặc hết lượt xử lý miễn phí tạm thời. Vui lòng thử lại sau ít phút."
        )


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "fashn_configured": is_valid_key(DEFAULT_FASHN_KEY),
        "rate_limit_per_window": RATE_LIMIT_PER_WINDOW,
        "rate_limit_window_sec": RATE_LIMIT_WINDOW_SEC,
    }


@app.post("/api/tryon")
async def tryon(
    request: Request,
    person: UploadFile = File(...),
    garment: UploadFile = File(...),
    category: str = Form("auto"),
    mode: str = Form("balanced"),
    prompt: str = Form(""),
):
    await enforce_rate_limit(request)

    allowed_cat = {"auto", "tops", "bottoms", "one-pieces"}
    allowed_mode = {"fast", "balanced", "quality"}
    if category not in allowed_cat or mode not in allowed_mode:
        raise HTTPException(400, "Tùy chọn loại trang phục hoặc chế độ AI không hợp lệ.")

    (person_bytes, _), (garment_bytes, _) = await asyncio.gather(
        read_image(person, "người mặc"),
        read_image(garment, "sản phẩm"),
    )

    if is_valid_key(DEFAULT_FASHN_KEY):
        img_uri = await run_fashn_ai(DEFAULT_FASHN_KEY, person_bytes, garment_bytes, category, mode, prompt)
        return {"ok": True, "image": img_uri, "engine_used": "FITAI Commercial Engine"}

    img_uri = await run_free_ai_space(person_bytes, garment_bytes, category, prompt, DEFAULT_HF_TOKEN)
    return {"ok": True, "image": img_uri, "engine_used": "FITAI Free AI Engine"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)
