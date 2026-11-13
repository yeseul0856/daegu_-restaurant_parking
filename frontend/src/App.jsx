import { useEffect, useMemo, useRef, useState } from 'react'
import { MapContainer, TileLayer, CircleMarker, Circle, Popup, Tooltip, useMap } from 'react-leaflet'
import 'leaflet/dist/leaflet.css'
import './App.css'
import { QUICK_FOODS, matchFood, searchTerms } from './foodSearch'

const DAEGU_CENTER = [35.8714, 128.6014]
const RADIUS_OPTIONS = [300, 500, 1000]

// 데이터별 마커 색상
const COLORS = {
  restaurant: '#ea580c', // 주황: 안심식당
  freeParking: '#16a34a', // 초록: 근처에 무료 주차장이 있는 안심식당
  selected: '#dc2626', // 빨강: 선택한 식당
  parking: '#2563eb', // 파랑: 주차장
}

const getJson = (url) =>
  fetch(url).then((r) => (r.ok ? r.json() : r.json().then((e) => Promise.reject(e.detail))))

const formatDistance = (m) => (m >= 1000 ? `${(m / 1000).toFixed(1)}km` : `${m}m`)

// 지도 이동 (MapContainer 내부에서만 useMap 사용 가능)
function FlyTo({ target }) {
  const map = useMap()
  useEffect(() => {
    if (target) map.flyTo(target, Math.max(map.getZoom(), 16), { duration: 0.6 })
  }, [target, map])
  return null
}

// 검색 결과가 모두 보이도록 지도 범위 조정
function FitResults({ points, trigger }) {
  const map = useMap()
  useEffect(() => {
    if (!trigger || points.length === 0) return
    if (points.length === 1) map.flyTo(points[0], 16, { duration: 0.6 })
    else map.flyToBounds(points, { padding: [40, 40], maxZoom: 16, duration: 0.6 })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [trigger, map])
  return null
}

function App() {
  const [data, setData] = useState(null)
  const [district, setDistrict] = useState('')
  const [query, setQuery] = useState('')
  const [onlyFreeParking, setOnlyFreeParking] = useState(false)
  const [selected, setSelected] = useState(null)
  const [radius, setRadius] = useState(500)
  const [nearby, setNearby] = useState(null)
  const [flyTarget, setFlyTarget] = useState(null)
  const [error, setError] = useState('')
  const parkingRefs = useRef({})

  // 대구 전체 안심식당 (+ 달서구 음식점 현황 일치 정보, 주변 무료 주차장)
  // 서버에서 주소→좌표 변환 중이면 다시 불러온다
  useEffect(() => {
    let timer
    const load = () =>
      getJson('/api/restaurants')
        .then((d) => {
          setData(d)
          const g = d.geocoding
          if (g.running || d.count === 0) timer = setTimeout(load, 10000)
          else if (g.error && !g.error.includes('인증키')) timer = setTimeout(load, 30000)
        })
        .catch((e) => setError(String(e)))
    load()
    return () => clearTimeout(timer)
  }, [])

  // 식당 선택 / 반경 변경 시 주변 주차장 조회
  useEffect(() => {
    if (!selected) return
    setNearby(null)
    getJson(`/api/parking/nearby?lat=${selected.lat}&lng=${selected.lng}&radius=${radius}&limit=50`)
      .then(setNearby)
      .catch((e) => setError(String(e)))
  }, [selected, radius])

  const items = data?.items ?? []
  const districts = useMemo(
    () => [...new Set(items.map((r) => r.district).filter(Boolean))].sort(),
    [items],
  )
  // 음식 검색 (식당 이름 + 등록 메뉴)
  const terms = useMemo(() => searchTerms(query), [query])
  const matches = useMemo(() => {
    const m = new Map()
    if (terms.length) for (const r of items) {
      const hit = matchFood(r, terms)
      if (hit) m.set(r.id, hit)
    }
    return m
  }, [items, terms])

  const shown = useMemo(
    () =>
      items.filter(
        (r) =>
          (!district || r.district === district) &&
          (!onlyFreeParking || r.freeParkingCount > 0) &&
          (!terms.length || matches.has(r.id)),
      ),
    [items, district, onlyFreeParking, terms, matches],
  )
  const fitKey = terms.length ? `${query}|${district}|${onlyFreeParking}|${shown.length}` : ''
  const fitPoints = useMemo(() => shown.map((r) => [r.lat, r.lng]), [shown])
  const freeCount = useMemo(
    () => items.filter((r) => (!district || r.district === district) && r.freeParkingCount > 0).length,
    [items, district],
  )

  const selectRestaurant = (r) => {
    setSelected(r)
    setFlyTarget([r.lat, r.lng])
  }

  const focusParking = (p) => {
    setFlyTarget([p.lat, p.lng])
    setTimeout(() => parkingRefs.current[p.id]?.openPopup(), 700)
  }

  const geo = data?.geocoding

  return (
    <div className="app">
      <MapContainer center={DAEGU_CENTER} zoom={12} preferCanvas className="map">
        <TileLayer
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
          url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
        />
        <FlyTo target={flyTarget} />
        <FitResults points={fitPoints} trigger={selected ? '' : fitKey} />

        {/* 안심식당 */}
        {shown.map((r) =>
          selected?.id === r.id ? null : (
            <CircleMarker
              key={`r-${r.id}`}
              center={[r.lat, r.lng]}
              radius={6}
              pathOptions={{
                color: '#fff',
                weight: 1,
                fillColor: r.freeParkingCount > 0 ? COLORS.freeParking : COLORS.restaurant,
                fillOpacity: 0.9,
              }}
              eventHandlers={{ click: () => selectRestaurant(r) }}
            >
              <Tooltip direction="top" offset={[0, -6]}>
                🍽 {r.name}
                {r.menu && ` · ${r.menu}`}
                {r.freeParkingCount > 0 && ' · 🅿 무료주차 근처'}
              </Tooltip>
            </CircleMarker>
          ),
        )}

        {/* 선택한 식당 + 검색 반경 + 주변 주차장 */}
        {selected && (
          <>
            <Circle
              center={[selected.lat, selected.lng]}
              radius={radius}
              pathOptions={{ color: COLORS.selected, weight: 1.5, dashArray: '6 6', fillOpacity: 0.05 }}
            />
            {nearby?.items.map((p) => (
              <CircleMarker
                key={`p-${p.id}`}
                ref={(el) => (parkingRefs.current[p.id] = el)}
                center={[p.lat, p.lng]}
                radius={8}
                pathOptions={{ color: '#fff', weight: 1.5, fillColor: COLORS.parking, fillOpacity: 0.9 }}
              >
                <Popup>
                  <strong style={{ color: COLORS.parking }}>🅿 {p.name}</strong>
                  <div>{p.address}</div>
                  <div>
                    {p.category} · {p.type} · 식당에서 {formatDistance(p.distance)}
                  </div>
                  <div>
                    주차구획 {p.spaces}면 · <b>{p.fee}</b>
                  </div>
                  {p.payment && <div>결제: {p.payment}</div>}
                </Popup>
              </CircleMarker>
            ))}
            <CircleMarker
              center={[selected.lat, selected.lng]}
              radius={10}
              pathOptions={{ color: '#fff', weight: 2, fillColor: COLORS.selected, fillOpacity: 1 }}
            >
              <Tooltip permanent direction="top" offset={[0, -10]}>
                🍽 {selected.name}
              </Tooltip>
            </CircleMarker>
          </>
        )}
      </MapContainer>

      <aside className="panel">
        <h1>대구 안심식당 · 주변 주차장</h1>

        <form className="search" onSubmit={(e) => e.preventDefault()}>
          <input
            type="search"
            value={query}
            onChange={(e) => { setQuery(e.target.value); setSelected(null); setNearby(null) }}
            placeholder="먹고 싶은 음식 검색 (예: 국밥, 고기, 회)"
            aria-label="음식 검색"
          />
        </form>
        <div className="chips">
          {QUICK_FOODS.map((f) => (
            <button
              key={f}
              className={query === f ? 'on' : ''}
              onClick={() => { setQuery(query === f ? '' : f); setSelected(null); setNearby(null) }}
            >
              {f}
            </button>
          ))}
        </div>

        <div className="legend">
          <span><i className="dot" style={{ background: COLORS.restaurant }} />안심식당</span>
          <span><i className="dot" style={{ background: COLORS.freeParking }} />무료주차 근처</span>
          <span><i className="dot" style={{ background: COLORS.selected }} />선택</span>
          <span><i className="dot" style={{ background: COLORS.parking }} />주차장</span>
        </div>

        {data && (
          <>
            <div className="filters">
              <select value={district} onChange={(e) => setDistrict(e.target.value)}>
                <option value="">대구 전체</option>
                {districts.map((d) => (
                  <option key={d} value={d}>{d}</option>
                ))}
              </select>
              <label className="filter">
                <input type="checkbox" checked={onlyFreeParking} onChange={(e) => setOnlyFreeParking(e.target.checked)} />
                {data.freeParkingRadius}m 안 무료주차 ({freeCount.toLocaleString()})
              </label>
            </div>
            <p className="note">
              안심식당 <b>{shown.length.toLocaleString()}곳</b> 표시 (전체 {data.total.toLocaleString()}곳)
              {(!district || district === '달서구') && (
                <>
                  <br />
                  달서구 {data.safeInDalseo}곳 중 {data.dalseoMatched}곳은 달서구 음식점 현황과 일치 → 상세정보 추가
                </>
              )}
              {geo?.running && (
                <>
                  <br />
                  주소→좌표 변환 중 ({geo.provider === 'vworld' ? '브이월드' : 'OpenStreetMap'}){' '}
                  {geo.done.toLocaleString()}/{geo.total.toLocaleString()}
                </>
              )}
            </p>
          </>
        )}
        {geo?.error && (
          <p className="error">
            {geo.error}
            {geo.error.includes('인증키') && ' — backend/.env 의 VWORLD_API_KEY 를 확인하세요.'}
          </p>
        )}
        {error && <p className="error">{error}</p>}

        {!selected && terms.length > 0 ? (
          <section className="detail">
            <p className="note">
              <b>'{query.trim()}'</b> 검색 결과 <b>{shown.length.toLocaleString()}곳</b>
              {terms.length > 1 && <> · 함께 찾은 단어: {terms.slice(1).join(', ')}</>}
              <br />
              식당 이름과 등록된 메뉴로 찾은 결과예요. (공공데이터에 전체 메뉴 정보는 없음)
            </p>
            {shown.length === 0 ? (
              <p className="hint">일치하는 식당이 없어요. 다른 음식 이름으로 검색해 보세요.</p>
            ) : (
              <ul className="list">
                {shown.slice(0, 200).map((r) => {
                  const hit = matches.get(r.id)
                  return (
                    <li key={r.id} onClick={() => selectRestaurant(r)}>
                      <div className="row">
                        <strong>{r.name}</strong>
                        <span className="tag">{hit.inMenu ? `메뉴: ${hit.term}` : hit.term}</span>
                      </div>
                      <div className="sub">
                        {r.district} · {r.address.replace(/^대구광역시\s*/, '')}
                        {r.menu && <> · {r.menu}</>}
                        {r.freeParkingCount > 0 && <span className="good"> · 🅿 무료주차</span>}
                      </div>
                    </li>
                  )
                })}
                {shown.length > 200 && <li className="sub">… 외 {shown.length - 200}곳 (구·군을 선택해 좁혀 보세요)</li>}
              </ul>
            )}
          </section>
        ) : !selected ? (
          <p className="hint">먹고 싶은 음식을 검색하거나, 지도에서 안심식당을 누르면 상세정보와 주변 주차장을 알려드려요.</p>
        ) : (
          <section className="detail">
            <div className="detail-head">
              <div>
                <h2>{selected.name}</h2>
                <div className="sub">{selected.address}</div>
                {selected.phone && <div className="sub">☎ {selected.phone}</div>}
                {selected.dalseo && (
                  <div className="sub">
                    {selected.dong} · {selected.category}
                    {selected.dalseoName && selected.dalseoName !== selected.name && ` · 등록 상호: ${selected.dalseoName}`}
                  </div>
                )}
              </div>
              <button className="close" onClick={() => { setSelected(null); setNearby(null) }} aria-label="선택 해제">
                ✕
              </button>
            </div>

            {terms.length > 0 && (
              <button className="back" onClick={() => { setSelected(null); setNearby(null) }}>
                ← '{query.trim()}' 검색 결과로
              </button>
            )}
            <div className="facts">
              {selected.dalseo && (
                <div>
                  <span className="label">메뉴</span>
                  {selected.menu ? (
                    <>
                      {selected.menu} <span className="badge">착한가격업소</span>
                    </>
                  ) : (
                    <span className="muted">공공데이터에 메뉴 정보 없음</span>
                  )}
                </div>
              )}
              <div>
                <span className="label">무료주차</span>
                {selected.freeParkingCount > 0 ? (
                  <span className="good">
                    {selected.freeParking[0].name} ({formatDistance(selected.freeParking[0].distance)})
                    {selected.freeParkingCount > 1 && ` 외 ${selected.freeParkingCount - 1}곳`}
                  </span>
                ) : (
                  <span className="muted">{data?.freeParkingRadius}m 안에 무료 주차장 없음</span>
                )}
              </div>
              {selected.dalseo && <div className="sub">✔ 달서구 음식점 현황에 등록된 업소</div>}
            </div>

            <div className="radius">
              반경
              {RADIUS_OPTIONS.map((r) => (
                <button key={r} className={r === radius ? 'on' : ''} onClick={() => setRadius(r)}>
                  {formatDistance(r)}
                </button>
              ))}
            </div>

            {!nearby ? (
              <p className="note">주차장 찾는 중…</p>
            ) : nearby.count === 0 ? (
              <p className="note">반경 {formatDistance(radius)} 안에 주차장이 없어요. 반경을 넓혀 보세요.</p>
            ) : (
              <>
                <p className="note">
                  주차장 {nearby.count}곳{nearby.count > nearby.items.length && ` (가까운 ${nearby.items.length}곳 표시)`}
                </p>
                <ul className="list">
                  {nearby.items.map((p) => (
                    <li key={p.id} onClick={() => focusParking(p)}>
                      <div className="row">
                        <strong>{p.name}</strong>
                        <span className="dist">{formatDistance(p.distance)}</span>
                      </div>
                      <div className="sub">
                        {p.category} · {p.type} · {p.spaces}면 ·{' '}
                        <span className={p.fee?.includes('무료') ? 'good' : ''}>{p.fee}</span>
                      </div>
                    </li>
                  ))}
                </ul>
              </>
            )}
          </section>
        )}
      </aside>
    </div>
  )
}

export default App
