"""대구광역시 통합 주차 정보 + 안심식당 현황 API 서버."""
import asyncio
import difflib
import json
import math
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from dotenv import dotenv_values, load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

SERVICE_KEY = os.getenv("ODCLOUD_SERVICE_KEY", "")
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://localhost:5173")

ODCLOUD_BASE = "https://api.odcloud.kr/api"
PARKING_PATH = "/15151311/v1/uddi:ddad927f-4d89-4eee-b6e1-a43c58e63195"
RESTAURANT_PATH = "/15116731/v1/uddi:cd77fe8a-440f-4673-b777-891ffbefdc47"

CACHE_TTL = 60 * 60  # 원본 데이터 메모리 캐시 1시간
# 주소 -> 좌표 캐시 (제공자별 파일). 도커에서는 CACHE_DIR=/data (볼륨) 로 지정
CACHE_DIR = Path(os.getenv("CACHE_DIR") or BASE_DIR)
CACHE_DIR.mkdir(parents=True, exist_ok=True)
GEOCODE_CACHE_FILES = {
    "vworld": CACHE_DIR / "geocode_cache_vworld.json",
    "osm": CACHE_DIR / "geocode_cache.json",
}
VWORLD_DELAY = 0.25  # 브이월드 요청 간격(초) - 너무 빠르면 502로 차단됨
VWORLD_MAX_FAILS = 5  # 연속 실패 허용 횟수 (넘으면 잠시 쉬었다가 재시도)
VWORLD_COOLDOWN = 5 * 60  # 연속 실패 시 쉬는 시간(초)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    load_geocode_cache()
    yield


app = FastAPI(title="Daegu Parking & Safe Restaurant API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN],
    allow_methods=["GET"],
    allow_headers=["*"],
)

_data_cache: dict[str, tuple[float, list[dict]]] = {}
_geocode_cache: dict[str, dict[str, list[float] | None]] = {"vworld": {}, "osm": {}}
_geocode_task: asyncio.Task | None = None
_geocode_progress = {"total": 0, "done": 0, "running": False, "provider": None, "error": None}
_failed_key: str | None = None  # 인증키 오류가 난 키 (키가 바뀌기 전까지 재시도 안 함)
_cooldown_until = 0.0  # 브이월드 서버 오류 시 이 시각까지 재시도하지 않음


# ---------------------------------------------------------------- odcloud
async def fetch_all(path: str) -> list[dict]:
    """odcloud API의 모든 페이지를 가져온다 (메모리 캐시)."""
    if not SERVICE_KEY or SERVICE_KEY.startswith("여기에"):
        raise HTTPException(500, "backend/.env 에 ODCLOUD_SERVICE_KEY 를 설정하세요.")

    cached = _data_cache.get(path)
    if cached and time.time() - cached[0] < CACHE_TTL:
        return cached[1]

    rows: list[dict] = []
    page, per_page = 1, 1000
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            res = await client.get(
                ODCLOUD_BASE + path,
                params={"page": page, "perPage": per_page, "serviceKey": SERVICE_KEY},
            )
            if res.status_code != 200:
                raise HTTPException(502, f"공공데이터 API 오류 {res.status_code}: {res.text[:200]}")
            body = res.json()
            rows.extend(body.get("data", []))
            if page * per_page >= body.get("totalCount", 0):
                break
            page += 1

    _data_cache[path] = (time.time(), rows)
    return rows


def to_float(v) -> float | None:
    try:
        f = float(v)
        return f if f != 0 else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- geocoding
class GeocodeKeyError(Exception):
    """인증키 오류 등 계속 진행해도 소용없는 오류."""


class GeocodeServerError(Exception):
    """502, 연결 끊김 등 일시적인 서버 오류."""


def vworld_key() -> str:
    """.env 를 매번 다시 읽어서 서버 재시작 없이 키 추가를 반영한다.

    .env 파일이 없으면(도커: env_file 로 주입) 환경변수를 사용한다.
    """
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        key = dotenv_values(env_file).get("VWORLD_API_KEY") or ""
    else:
        key = os.getenv("VWORLD_API_KEY", "")
    key = key.strip()
    return "" if key.startswith("여기에") else key


def load_geocode_cache() -> None:
    for provider, path in GEOCODE_CACHE_FILES.items():
        if path.exists():
            _geocode_cache[provider].update(json.loads(path.read_text(encoding="utf-8")))


def save_geocode_cache(provider: str) -> None:
    GEOCODE_CACHE_FILES[provider].write_text(
        json.dumps(_geocode_cache[provider], ensure_ascii=False), encoding="utf-8"
    )


def lookup_coord(address: str) -> list[float] | None:
    """브이월드 결과를 우선 사용하고, 없으면 OSM 결과를 사용."""
    return _geocode_cache["vworld"].get(address) or _geocode_cache["osm"].get(address)


def pending_addresses(addresses: list[str], provider: str) -> list[str]:
    if provider == "vworld":
        return [a for a in addresses if a not in _geocode_cache["vworld"]]
    return [a for a in addresses if a not in _geocode_cache["osm"] and not lookup_coord(a)]


async def geocode_vworld(client: httpx.AsyncClient, address: str, key: str) -> list[float] | None:
    """브이월드 지오코더 2.0: 도로명 주소로 먼저, 못 찾으면 지번 주소로 검색."""
    for addr_type in ("road", "parcel"):
        try:
            res = await client.get(
                "https://api.vworld.kr/req/address",
                params={
                    "service": "address",
                    "request": "getcoord",
                    "version": "2.0",
                    "crs": "epsg:4326",
                    "type": addr_type,
                    "address": address,
                    "refine": "true",
                    "simple": "false",
                    "format": "json",
                    "key": key,
                },
            )
            body = res.json().get("response", {})
        except (httpx.HTTPError, ValueError) as e:
            raise GeocodeServerError(f"{type(e).__name__}") from e
        finally:
            await asyncio.sleep(VWORLD_DELAY)
        status = body.get("status")
        if status == "OK":
            point = body["result"]["point"]
            return [float(point["y"]), float(point["x"])]
        if status == "ERROR":
            err = body.get("error", {})
            raise GeocodeKeyError(f"브이월드 오류 {err.get('code')}: {err.get('text')}")
        # NOT_FOUND -> 다음 주소 유형으로 재시도
    return None


async def geocode_osm(client: httpx.AsyncClient, address: str) -> list[float] | None:
    """OpenStreetMap Nominatim (키 불필요, 초당 1회 제한).

    건물번호까지 못 찾으면 번호를 떼고 도로/동 단위로 한 번 더 찾는다.
    """
    candidates = [address]
    parts = address.split()
    if len(parts) > 2 and any(ch.isdigit() for ch in parts[-1]):
        candidates.append(" ".join(parts[:-1]))

    for query in candidates:
        await asyncio.sleep(1.1)
        res = await client.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 1, "countrycodes": "kr"},
            headers={"User-Agent": "daegu-parking-map-demo/1.0"},
        )
        items = res.json() if res.status_code == 200 else []
        if items:
            return [float(items[0]["lat"]), float(items[0]["lon"])]
    return None


def clean_address(addr: str) -> str:
    """괄호 안 부가정보, 층/호수 등을 잘라 지오코딩 성공률을 높인다."""
    addr = addr.split("(")[0].split(",")[0].strip()
    if not addr.startswith("대구"):
        addr = "대구광역시 " + addr
    return addr


def restaurant_addresses(rows: list[dict]) -> list[str]:
    # 데이터 순서대로, 중복 주소 제거
    return list(dict.fromkeys(clean_address(r["주소1"]) for r in rows if r.get("주소1")))


async def geocode_restaurants(rows: list[dict]) -> None:
    global _failed_key, _cooldown_until
    key = vworld_key()
    provider = "vworld" if key else "osm"
    addresses = restaurant_addresses(rows)
    todo = pending_addresses(addresses, provider)
    _geocode_progress.update(
        total=len(addresses), done=len(addresses) - len(todo),
        running=True, provider=provider, error=None,
    )
    cache = _geocode_cache[provider]
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            if provider == "vworld":
                fails = 0
                for i, addr in enumerate(todo, 1):
                    try:
                        cache[addr] = await geocode_vworld(client, addr, key)
                    except GeocodeServerError:
                        fails += 1
                        if fails >= VWORLD_MAX_FAILS:
                            raise
                        await asyncio.sleep(2 ** fails)  # 2, 4, 8, 16초 후 재시도
                        continue
                    fails = 0
                    _geocode_progress["done"] += 1
                    if i % 50 == 0:
                        save_geocode_cache(provider)
            else:
                for i, addr in enumerate(todo, 1):
                    if vworld_key():  # 도중에 브이월드 키가 생기면 중단 -> 다음 요청 때 브이월드로 재시작
                        break
                    try:
                        cache[addr] = await geocode_osm(client, addr)
                    except (httpx.HTTPError, ValueError):
                        continue
                    _geocode_progress["done"] += 1
                    if i % 20 == 0:
                        save_geocode_cache(provider)
    except GeocodeKeyError as e:
        _geocode_progress["error"] = str(e)
        _failed_key = key
    except GeocodeServerError:
        _cooldown_until = time.time() + VWORLD_COOLDOWN
        _geocode_progress["error"] = (
            f"브이월드 서버가 응답하지 않습니다(일시적). {VWORLD_COOLDOWN // 60}분 후 자동으로 이어서 변환합니다."
        )
    finally:
        save_geocode_cache(provider)
        _geocode_progress["running"] = False


# ---------------------------------------------------------------- routes
def distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """두 좌표 사이 거리(m) - 하버사인 공식."""
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


async def parking_items() -> list[dict]:
    rows = await fetch_all(PARKING_PATH)
    result = []
    for r in rows:
        lat, lng = to_float(r.get("위도")), to_float(r.get("경도"))
        if lat is None or lng is None:
            continue
        result.append({
            "id": r.get("연번"),
            "name": r.get("주차장명"),
            "address": r.get("지번주소"),
            "district": r.get("시군구명"),
            "type": r.get("주차장유형"),
            "category": r.get("주차장구분"),
            "spaces": r.get("주차구획수"),
            "fee": r.get("요금부과구분명"),
            "payment": r.get("결제방법"),
            "lat": lat,
            "lng": lng,
        })
    return result


@app.get("/api/parking")
async def parking():
    items = await parking_items()
    return {"count": len(items), "items": items}


@app.get("/api/parking/nearby")
async def parking_nearby(
    lat: float,
    lng: float,
    radius: int = Query(500, ge=100, le=5000, description="검색 반경(m)"),
    limit: int = Query(20, ge=1, le=100),
):
    """주어진 좌표 반경 안의 주차장을 가까운 순으로 반환."""
    items = []
    for p in await parking_items():
        d = distance_m(lat, lng, p["lat"], p["lng"])
        if d <= radius:
            items.append({**p, "distance": round(d)})
    items.sort(key=lambda p: p["distance"])
    return {"count": len(items), "radius": radius, "items": items[:limit]}


# ---------------------------------------------------------------- 달서구 음식점 현황 매칭
DALSEO_PATH = "/3071544/v1/uddi:1c67b132-c9c8-4427-929e-e35429f9d1ba"  # 2026-10-29 (최신)
DALSEO_MENU_PATH = "/3071544/v1/uddi:6c7f534d-ab9b-47fe-b5a1-df0ff871baf5"  # 2025-05-27 (식단 항목 있음)
FREE_PARKING_RADIUS = 200  # 이 반경(m) 안에 무료 주차장이 있으면 "무료주차 근처"로 표시
GRID = 0.005  # 무료 주차장 격자 크기(도) ≈ 450~550m, FREE_PARKING_RADIUS 보다 커야 함


def norm_name(name: str | None) -> str:
    """상호 비교용: 괄호 내용, 공백, 특수문자 제거."""
    name = re.sub(r"\(.*?\)|\[.*?\]", "", name or "")
    return re.sub(r"[^0-9A-Za-z가-힣]", "", name).lower()


def road_key(addr: str | None) -> str | None:
    """'대구광역시 달서구 이곡서로 37-1, 1층 (이곡동)' -> '이곡서로 37-1'"""
    addr = (addr or "").split(",")[0].split("(")[0]
    m = re.search(r"([가-힣0-9.]+(?:로|길))\s*(\d+(?:-\d+)?)", addr)
    return f"{m.group(1)} {m.group(2)}" if m else None


def same_shop(a: str, b: str) -> bool:
    """같은 주소에 있는 두 상호가 같은 가게인지 (지점명 차이, 오타 허용)."""
    a, b = norm_name(a), norm_name(b)
    if not a or not b:
        return False
    return a == b or a in b or b in a or difflib.SequenceMatcher(None, a, b).ratio() >= 0.7


def find_shop(index: dict[str, list[dict]], name: str, addr: str) -> dict | None:
    key = road_key(addr)
    for d in index.get(key, []) if key else []:
        if same_shop(name, d.get("업소명")):
            return d
    return None


def index_by_road(rows: list[dict]) -> dict[str, list[dict]]:
    index: dict[str, list[dict]] = {}
    for d in rows:
        key = road_key(d.get("소재지(도로명)"))
        if key:
            index.setdefault(key, []).append(d)
    return index


def grid_cell(lat: float, lng: float) -> tuple[int, int]:
    return int(lat // GRID), int(lng // GRID)


def build_grid(parks: list[dict]) -> dict[tuple[int, int], list[dict]]:
    grid: dict[tuple[int, int], list[dict]] = {}
    for p in parks:
        grid.setdefault(grid_cell(p["lat"], p["lng"]), []).append(p)
    return grid


def free_parking_near(grid: dict, lat: float, lng: float) -> list[dict]:
    """FREE_PARKING_RADIUS 안의 무료 주차장 (가까운 순). 주변 3x3 격자만 검사."""
    ci, cj = grid_cell(lat, lng)
    found = []
    for di in (-1, 0, 1):
        for dj in (-1, 0, 1):
            for p in grid.get((ci + di, cj + dj), []):
                d = round(distance_m(lat, lng, p["lat"], p["lng"]))
                if d <= FREE_PARKING_RADIUS:
                    found.append({"name": p["name"], "distance": d})
    return sorted(found, key=lambda p: p["distance"])


# ---------------------------------------------------------------- 안심식당
@app.get("/api/restaurants")
async def restaurants():
    """대구 전체 안심식당.

    - 달서구 음식점 현황과 일치하는 식당은 행정동/업종/메뉴/정확한 좌표를 덧붙임
    - 모든 식당에 반경 FREE_PARKING_RADIUS 안의 무료 주차장 정보를 덧붙임
    """
    global _geocode_task
    rows, dalseo_rows, menu_rows, parks = await asyncio.gather(
        fetch_all(RESTAURANT_PATH),
        fetch_all(DALSEO_PATH),
        fetch_all(DALSEO_MENU_PATH),
        parking_items(),
    )

    # 좌표가 없는 주소가 있으면 백그라운드 지오코딩 시작
    # (인증키 오류가 난 키로는 키가 바뀔 때까지 재시도하지 않음)
    if _geocode_task is None or _geocode_task.done():
        key = vworld_key()
        provider = "vworld" if key else "osm"
        cooling = key and time.time() < _cooldown_until
        if not cooling and not (key and key == _failed_key) and pending_addresses(restaurant_addresses(rows), provider):
            _geocode_task = asyncio.create_task(geocode_restaurants(rows))

    dalseo_index = index_by_road(dalseo_rows)
    menu_index = index_by_road([r for r in menu_rows if (r.get("식단") or "").strip()])
    free_grid = build_grid([p for p in parks if "무료" in (p.get("fee") or "")])

    result = []
    safe_in_dalseo = matched = 0
    for r in rows:
        addr = r.get("주소1")
        if not addr:
            continue
        name = r.get("안심식당명")
        item = {
            "id": r.get("연번"),
            "name": name,
            "phone": r.get("전화번호"),
            "address": addr,
            "designated": r.get("지정일"),
            "district": next((g for g in addr.split() if g.endswith(("구", "군"))), None),
            "dalseo": False,
        }

        coord = lookup_coord(clean_address(addr))
        if "달서구" in addr:
            safe_in_dalseo += 1
            shop = find_shop(dalseo_index, name, addr)
            if shop:
                matched += 1
                menu_shop = find_shop(menu_index, name, addr)
                item.update({
                    "dalseo": True,  # 달서구 음식점 현황과 일치
                    "dalseoName": shop.get("업소명"),
                    "dong": shop.get("행정동명"),
                    "category": shop.get("업종명"),
                    "menu": (menu_shop or {}).get("식단"),
                })
                lat, lng = to_float(shop.get("위도")), to_float(shop.get("경도"))
                if lat is not None and lng is not None:
                    coord = [lat, lng]  # 달서구 데이터의 좌표가 더 정확

        if not coord:
            continue
        near = free_parking_near(free_grid, coord[0], coord[1])
        item.update({
            "lat": coord[0],
            "lng": coord[1],
            "freeParking": near[:5],
            "freeParkingCount": len(near),
        })
        result.append(item)

    return {
        "count": len(result),
        "total": len(rows),
        "safeInDalseo": safe_in_dalseo,
        "dalseoMatched": matched,
        "freeParkingRadius": FREE_PARKING_RADIUS,
        "geocoding": dict(_geocode_progress),
        "items": result,
    }


@app.get("/api/health")
async def health():
    return {"ok": True, "serviceKey": bool(SERVICE_KEY), "vworldKey": bool(vworld_key())}
