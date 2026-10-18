"""GreenRoute AI - carbon-aware router for LLM inference.

For every prompt it decides WHICH model size, WHERE (cloud region) and WHEN
(now vs. a greener hour) to run it, then executes it on Gemini (Vertex AI) and
logs the carbon accounting to Firestore.
"""
import base64
import datetime as dt
import json
import logging
import math
import os
import threading
import time
import uuid

import firebase_admin
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from firebase_admin import firestore
from google import genai
from google.cloud import bigquery, pubsub_v1
from google.genai import types
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("greenroute")

PROJECT = os.environ.get("PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT")
DATASET = os.environ.get("BQ_DATASET", "greenroute")
TOPIC = os.environ.get("PUBSUB_TOPIC", "greenroute-jobs")
BASELINE_REGION = os.environ.get("BASELINE_REGION", "asia-south1")
PUE = 1.09  # Google fleet-wide trailing PUE

# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------
# Fallback copy of Google's published grid carbon intensity (2023, gCO2eq/kWh).
# Live values are loaded from BigQuery table `region_carbon` at startup.
REGION_CARBON = {
    "northamerica-northeast1": ("Montréal", 2, 1.00),
    "europe-west9": ("Paris", 34, 0.94),
    "europe-north1": ("Finland", 46, 0.98),
    "us-west1": ("Oregon", 94, 0.84),
    "europe-west1": ("Belgium", 122, 0.82),
    "europe-west4": ("Netherlands", 236, 0.80),
    "us-central1": ("Iowa", 430, 0.95),
    "asia-northeast1": ("Tokyo", 459, 0.16),
    "asia-southeast1": ("Singapore", 368, 0.04),
    "asia-south1": ("Mumbai", 648, 0.14),
    "asia-south2": ("Delhi", 645, 0.12),
}
UTC_OFFSET = {
    "northamerica-northeast1": -4, "europe-west9": 2, "europe-north1": 3,
    "us-west1": -7, "europe-west1": 2, "europe-west4": 2, "us-central1": -5,
    "asia-northeast1": 9, "asia-southeast1": 8, "asia-south1": 5.5, "asia-south2": 5.5,
}
INDIA_REGIONS = ["asia-south1", "asia-south2"]

# Energy per 1k OUTPUT tokens (Wh, accelerator + host); input tokens cost ~1/10.
# Calibrated so a typical short prompt lands near Google's published median of
# 0.24 Wh per Gemini text prompt (Aug 2025 methodology paper).
TIERS = {
    "lite":  {"wh_per_1k": 0.25, "label": "Flash-Lite (small)"},
    "flash": {"wh_per_1k": 0.60, "label": "Flash (medium)"},
    "pro":   {"wh_per_1k": 2.50, "label": "Pro (large)"},
}
COMPLEXITY_TO_TIER = {"simple": "lite", "medium": "flash", "complex": "pro"}


def _env_list(name, default):
    v = os.environ.get(name)
    return [x.strip() for x in v.split(",")] if v else default


MODEL_CANDIDATES = {
    "lite": _env_list("MODELS_LITE", ["gemini-2.5-flash-lite", "gemini-3.1-flash-lite-preview",
                                      "gemini-3-flash-lite", "gemini-2.0-flash-lite-001"]),
    "flash": _env_list("MODELS_FLASH", ["gemini-2.5-flash", "gemini-3-flash-preview",
                                        "gemini-3-flash", "gemini-2.0-flash-001"]),
    "pro": _env_list("MODELS_PRO", ["gemini-2.5-pro", "gemini-3-pro-preview",
                                    "gemini-3.1-pro-preview", "gemini-3-pro"]),
}

# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------
firebase_admin.initialize_app(options={"projectId": PROJECT})
db = firestore.client()
bq = bigquery.Client(project=PROJECT)
publisher = pubsub_v1.PublisherClient()
topic_path = publisher.topic_path(PROJECT, TOPIC)
_genai_clients = {}
_good = {}   # (tier, location) -> model name that works
_bad = set()  # (model, location) pairs that failed
_lock = threading.Lock()


def gclient(location):
    if location not in _genai_clients:
        _genai_clients[location] = genai.Client(vertexai=True, project=PROJECT, location=location)
    return _genai_clients[location]


def bq_rows(sql):
    try:
        return [dict(r) for r in bq.query(sql).result()]
    except Exception as e:  # keep the app usable even if a table is missing
        log.warning("BigQuery query failed: %s", e)
        return []


def load_region_carbon():
    rows = bq_rows(f"SELECT region, location, cfe, intensity FROM `{PROJECT}.{DATASET}.region_carbon`")
    for r in rows:
        if r["region"] in REGION_CARBON:
            REGION_CARBON[r["region"]] = (r["location"], float(r["intensity"]), float(r["cfe"]))
    log.info("Loaded %d region carbon rows from BigQuery", len(rows))


# Output-length predictor trained with BigQuery ML (registered in Vertex AI
# Model Registry). We pull its weights once and score in-process.
OUTLEN = {"intercept": 4.0, "log_ctx": 0.0}


def load_outlen_model():
    rows = bq_rows(f"SELECT processed_input, weight FROM ML.WEIGHTS(MODEL `{PROJECT}.{DATASET}.outlen_model`)")
    for r in rows:
        if r["processed_input"] == "__INTERCEPT__":
            OUTLEN["intercept"] = r["weight"]
        elif r["processed_input"] == "log_ctx":
            OUTLEN["log_ctx"] = r["weight"]
    if rows:
        OUTLEN["loaded"] = True
    log.info("Out-length model weights: %s", OUTLEN)


def predict_out_tokens(ctx_tokens):
    v = OUTLEN["intercept"] + OUTLEN["log_ctx"] * math.log1p(ctx_tokens)
    return int(max(1, min(8000, math.expm1(v))))


# ---------------------------------------------------------------------------
# Carbon model
# ---------------------------------------------------------------------------
def intensity_at(region, when_utc):
    """Hourly grid intensity, modelled from the annual average with a solar-dip
    daily profile (deeper for fossil-heavy grids, flat for hydro/nuclear)."""
    loc, annual, cfe = REGION_CARBON[region]
    local_h = (when_utc.hour + when_utc.minute / 60 + UTC_OFFSET.get(region, 0)) % 24
    amp = 0.04 + 0.14 * (1 - cfe)
    return max(0.0, annual * (1 - amp * math.cos(2 * math.pi * (local_h - 13) / 24)))


def energy_wh(tier, in_tok, out_tok):
    c = TIERS[tier]["wh_per_1k"]
    return PUE * (in_tok * c / 10 + out_tok * c) / 1000


def co2_g(wh, intensity):
    return wh / 1000 * intensity


def allowed_regions(residency):
    return INDIA_REGIONS if residency == "india" else list(REGION_CARBON)


def plan_slot(regions, deferrable, deadline_h):
    now = dt.datetime.now(dt.timezone.utc)
    best = None
    steps = int(deadline_h * 2) if deferrable else 0  # 30-minute slots
    for s in range(steps + 1):
        t = now + dt.timedelta(minutes=30 * s)
        for r in regions:
            i = intensity_at(r, t)
            if best is None or i < best[2] - 1e-9:
                best = (r, t, i)
    return best


# ---------------------------------------------------------------------------
# Gemini helpers
# ---------------------------------------------------------------------------
def generate(tier, locations, contents, config=None, max_attempts=10):
    """Run on the greenest location where a model of this tier is available."""
    attempts = 0
    last_err = None
    for loc in list(locations) + ["global"]:
        cands = [_good[(tier, loc)]] if (tier, loc) in _good else MODEL_CANDIDATES[tier]
        for model in cands:
            if (model, loc) in _bad:
                continue
            attempts += 1
            if attempts > max_attempts:
                break
            try:
                resp = gclient(loc).models.generate_content(model=model, contents=contents, config=config)
                with _lock:
                    _good[(tier, loc)] = model
                return resp, model, loc
            except Exception as e:
                last_err = e
                msg = str(e)
                log.info("model %s @ %s failed: %s", model, loc, msg[:160])
                if "404" in msg or "NOT_FOUND" in msg or "not found" in msg.lower() or "400" in msg:
                    with _lock:
                        _bad.add((model, loc))
    raise HTTPException(502, f"No Gemini model available for tier {tier}: {last_err}")


CLASSIFY_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "complexity": {"type": "STRING", "enum": ["simple", "medium", "complex"]},
        "est_output_tokens": {"type": "INTEGER"},
        "reason": {"type": "STRING"},
    },
    "required": ["complexity", "est_output_tokens", "reason"],
}


def classify(prompt, has_image):
    instr = (
        "You are the triage brain of a carbon-aware LLM router. Classify how much model "
        "capability this request needs. simple = facts, short rewrites, greetings, "
        "translation, classification. medium = summaries, emails, standard code, "
        "explanations. complex = multi-step reasoning, math proofs, architecture, long "
        "creative work, deep analysis. Prefer the SMALLEST tier that will answer well. "
        "Estimate the answer length in tokens."
    )
    text = f"Request{' (with an attached image)' if has_image else ''}:\n\"\"\"{prompt[:4000]}\"\"\""
    cfg = types.GenerateContentConfig(
        system_instruction=instr, temperature=0, response_mime_type="application/json",
        response_schema=CLASSIFY_SCHEMA)
    resp, model, loc = generate("lite", ["us-central1"], [text], cfg)
    try:
        out = json.loads(resp.text)
    except Exception:
        out = {"complexity": "medium", "est_output_tokens": 300, "reason": "fallback"}
    out["triage_model"] = model
    return out


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
app = FastAPI(title="GreenRoute AI")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
def startup():
    threading.Thread(target=lambda: (load_region_carbon(), load_outlen_model()), daemon=True).start()


class RouteRequest(BaseModel):
    prompt: str
    deferrable: bool = False
    deadline_hours: float = 6
    residency: str = "anywhere"  # anywhere | india
    force_tier: str | None = None
    image_b64: str | None = None
    image_mime: str | None = None


def build_contents(prompt, image_b64, image_mime):
    parts = []
    if image_b64:
        parts.append(types.Part.from_bytes(data=base64.b64decode(image_b64), mime_type=image_mime or "image/png"))
    parts.append(prompt)
    return parts


def execute(job):
    """Run the prompt and write carbon accounting. Used directly and by the Pub/Sub worker."""
    contents = build_contents(job["prompt"], job.get("image_b64"), job.get("image_mime"))
    regions = sorted(job["regions"], key=lambda r: intensity_at(r, dt.datetime.fromisoformat(job["slot"])))
    t0 = time.time()
    resp, model, loc = generate(job["tier"], regions, contents,
                                types.GenerateContentConfig(temperature=0.4, max_output_tokens=2048))
    latency = time.time() - t0
    um = resp.usage_metadata
    in_tok = (um.prompt_token_count or 0) if um else 0
    out_tok = ((um.candidates_token_count or 0) + (getattr(um, "thoughts_token_count", 0) or 0)) if um else 0
    slot = dt.datetime.fromisoformat(job["slot"])
    acct_region = loc if loc in REGION_CARBON else "us-central1"
    inten = intensity_at(acct_region, slot)
    wh = energy_wh(job["tier"], in_tok, out_tok)
    g = co2_g(wh, inten)
    base_wh = energy_wh("pro", in_tok, out_tok)
    base_g = co2_g(base_wh, intensity_at(BASELINE_REGION, dt.datetime.now(dt.timezone.utc)))
    saved = max(0.0, base_g - g)
    result = {
        "status": "done",
        "answer": (resp.text or "")[:6000],
        "model": model,
        "served_location": loc,
        "accounted_region": acct_region,
        "region_name": REGION_CARBON[acct_region][0],
        "intensity": round(inten, 1),
        "in_tokens": in_tok,
        "out_tokens": out_tok,
        "energy_wh": round(wh, 4),
        "co2_g": round(g, 4),
        "baseline_co2_g": round(base_g, 4),
        "saved_g": round(saved, 4),
        "saved_pct": round(100 * saved / base_g, 1) if base_g else 0,
        "latency_s": round(latency, 2),
        "completed_at": firestore.SERVER_TIMESTAMP,
    }
    db.collection("requests").document(job["id"]).set(result, merge=True)
    db.collection("stats").document("global").set({
        "requests": firestore.Increment(1),
        "co2_g": firestore.Increment(g),
        "baseline_co2_g": firestore.Increment(base_g),
        "saved_g": firestore.Increment(saved),
        "energy_wh": firestore.Increment(wh),
        "deferred": firestore.Increment(1 if job.get("deferrable") else 0),
    }, merge=True)
    return result


@app.post("/api/route")
def route(req: RouteRequest):
    if not req.prompt.strip():
        raise HTTPException(400, "Empty prompt")
    triage = classify(req.prompt, bool(req.image_b64))
    tier = req.force_tier if req.force_tier in TIERS else COMPLEXITY_TO_TIER.get(triage["complexity"], "flash")
    ctx_est = len(req.prompt) // 4 + (258 if req.image_b64 else 0)
    pred_out = predict_out_tokens(ctx_est)
    regions = allowed_regions(req.residency)
    region, slot, inten = plan_slot(regions, req.deferrable, req.deadline_hours)
    now = dt.datetime.now(dt.timezone.utc)
    est_wh = energy_wh(tier, ctx_est, pred_out)
    plan = {
        "complexity": triage["complexity"],
        "triage_reason": triage.get("reason", ""),
        "triage_model": triage.get("triage_model"),
        "gemini_est_out_tokens": triage.get("est_output_tokens"),
        "vertex_pred_out_tokens": pred_out,
        "tier": tier,
        "tier_label": TIERS[tier]["label"],
        "planned_region": region,
        "planned_region_name": REGION_CARBON[region][0],
        "planned_intensity": round(inten, 1),
        "now_intensity_baseline": round(intensity_at(BASELINE_REGION, now), 1),
        "slot": slot.isoformat(),
        "deferred_by_min": int((slot - now).total_seconds() // 60),
        "est_energy_wh": round(est_wh, 4),
        "est_co2_g": round(co2_g(est_wh, inten), 4),
        "est_baseline_co2_g": round(co2_g(energy_wh("pro", ctx_est, pred_out), intensity_at(BASELINE_REGION, now)), 4),
    }
    job_id = uuid.uuid4().hex[:12]
    doc = {
        "created_at": firestore.SERVER_TIMESTAMP,
        "prompt_preview": req.prompt[:90],
        "residency": req.residency,
        "deferrable": req.deferrable,
        "has_image": bool(req.image_b64),
        "status": "scheduled" if req.deferrable else "running",
        **plan,
    }
    db.collection("requests").document(job_id).set(doc)
    job = {"id": job_id, "prompt": req.prompt, "tier": tier, "regions": regions,
           "slot": slot.isoformat(), "deferrable": req.deferrable,
           "image_b64": req.image_b64, "image_mime": req.image_mime}
    if req.deferrable:
        # Event-driven path: the job goes to Pub/Sub; the worker executes it.
        publisher.publish(topic_path, json.dumps(job).encode(), job_id=job_id).result(timeout=10)
        return {"id": job_id, "plan": plan, "status": "scheduled"}
    try:
        result = execute(job)
    except HTTPException as e:
        db.collection("requests").document(job_id).set({"status": "failed", "error": str(e.detail)}, merge=True)
        raise
    return {"id": job_id, "plan": plan, "status": "done", "result": {k: v for k, v in result.items() if k != "completed_at"}}


@app.post("/api/worker")
async def worker(request: Request):
    """Pub/Sub push endpoint for deferred jobs.

    Demo mode: the job runs on arrival, but its carbon is accounted at the
    scheduled green slot. In production this would be a Cloud Tasks ETA."""
    envelope = await request.json()
    try:
        job = json.loads(base64.b64decode(envelope["message"]["data"]))
        execute(job)
    except Exception as e:
        log.exception("worker failed")
        try:
            db.collection("requests").document(job["id"]).set({"status": "failed", "error": str(e)[:300]}, merge=True)
        except Exception:
            pass
    return Response(status_code=204)  # ack either way to avoid retry storms


@app.get("/api/regions")
def regions():
    now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    out = []
    for r, (loc, annual, cfe) in REGION_CARBON.items():
        out.append({
            "region": r, "location": loc, "annual_intensity": annual, "cfe": cfe,
            "now": round(intensity_at(r, now), 1),
            "next24": [round(intensity_at(r, now + dt.timedelta(hours=h)), 1) for h in range(24)],
        })
    return {"start": now.isoformat(), "regions": sorted(out, key=lambda x: x["now"])}


_cache = {}


def cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


@app.get("/api/fleet")
def fleet():
    """Replay of 1 week of real Azure production LLM traffic through our policy (BigQuery)."""
    def q():
        summary = bq_rows(f"SELECT * FROM `{PROJECT}.{DATASET}.fleet_summary`")
        tiers = bq_rows(f"SELECT * FROM `{PROJECT}.{DATASET}.fleet_by_tier` ORDER BY tier")
        hours = bq_rows(f"SELECT * FROM `{PROJECT}.{DATASET}.fleet_by_hour` ORDER BY hour")
        return {"summary": summary[0] if summary else None, "tiers": tiers, "hours": hours}
    return cached("fleet", 3600, q)


@app.get("/api/forecast")
def forecast():
    """Next-24h request demand forecast from the BigQuery ML ARIMA_PLUS model."""
    def q():
        hist = bq_rows(f"""SELECT FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M', ts) ts, requests
                           FROM `{PROJECT}.{DATASET}.demand_10min`
                           WHERE ts >= TIMESTAMP_SUB((SELECT MAX(ts) FROM `{PROJECT}.{DATASET}.demand_10min`), INTERVAL 48 HOUR)
                           ORDER BY ts""")
        fc = bq_rows(f"""SELECT FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M', ts) ts, ROUND(forecast) forecast,
                                ROUND(lower) lower, ROUND(upper) upper
                         FROM `{PROJECT}.{DATASET}.demand_forecast` ORDER BY ts""")
        return {"history": hist, "forecast": fc}
    return cached("forecast", 3600, q)


class AskRequest(BaseModel):
    question: str


@app.post("/api/ask")
def ask(req: AskRequest):
    """Natural-language interface over the sustainability analytics."""
    stats = db.collection("stats").document("global").get().to_dict() or {}
    ctx = {
        "live_stats_from_firestore": stats,
        "fleet_replay_bigquery": fleet(),
        "regions_now": regions()["regions"][:11],
        "energy_model_wh_per_1k_output_tokens": {k: v["wh_per_1k"] for k, v in TIERS.items()},
        "baseline": f"every request on the large model in {BASELINE_REGION}",
    }
    instr = ("You are GreenRoute's sustainability analyst. Answer using ONLY the JSON data given. "
             "Be concise (under 150 words), quantify with numbers, use markdown bullets. "
             "Relatable equivalents are welcome (1 km petrol car ≈ 120 g CO2; one smartphone charge ≈ 8 g CO2).")
    resp, model, loc = generate("flash", ["us-central1"],
                                [f"DATA:\n{json.dumps(ctx, default=str)[:30000]}\n\nQUESTION: {req.question}"],
                                types.GenerateContentConfig(system_instruction=instr, temperature=0.3))
    return {"answer": resp.text, "model": model}


@app.get("/api/warmup")
def warmup():
    """Resolve which Gemini model names work in which regions (cached)."""
    resolved = {t for t, _ in _good}
    for tier in TIERS:
        if tier in resolved:
            continue
        try:
            generate(tier, ["europe-north1", "us-central1", "asia-south1"], ["Say OK"],
                     types.GenerateContentConfig(max_output_tokens=5))
        except Exception as e:
            log.warning("warmup %s: %s", tier, e)
    return {"good": {f"{k[0]}@{k[1]}": v for k, v in _good.items()}, "outlen_model": OUTLEN}


@app.get("/api/health")
def health():
    return {"ok": True, "project": PROJECT}


# Static frontend (also served via Firebase Hosting; this keeps the Cloud Run URL usable on its own).
STATIC_DIR = os.path.join(os.path.dirname(__file__), "public")
if os.path.isdir(STATIC_DIR):
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
