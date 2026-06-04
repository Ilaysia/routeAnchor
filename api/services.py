import os
import aiohttp
import traceback
import itertools
import urllib.parse
import re
import asyncio  
from fastapi import HTTPException
from api.schemas import RouteRequest, RouteResponse, RouteSegment, LocationPoint, Coordinate, TransitOption

# 프록시 환경변수 제거
for key in ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY']:
    os.environ.pop(key, None)

TMAP_API_KEY = os.environ.get("TMAP_API_KEY")
TAGO_API_KEY = os.environ.get("TAGO_API_KEY")
SEOUL_SUBWAY_API_KEY = os.environ.get("SEOUL_SUBWAY_API_KEY")

# =====================================================================
# 🌟 [최적화 1] 지하철역 하나당 1번만 호출하여 전체 호선 시간표를 한 번에 캐싱
# =====================================================================
async def fetch_seoul_subway_all(station_name: str, session: aiohttp.ClientSession) -> tuple:
    if not SEOUL_SUBWAY_API_KEY: return station_name, {}
    
    subway_key = SEOUL_SUBWAY_API_KEY.strip()
    clean_name = re.split(r'역|\(|\.|·', station_name)[0].strip()
    encoded_name = urllib.parse.quote(clean_name)
    url = f"http://swopenapi.seoul.go.kr/api/subway/{subway_key}/json/realtimeStationArrival/0/15/{encoded_name}"
    
    try:
        async with session.get(url, timeout=2.5) as response:
            if response.status == 200:
                data = await response.json()
                realtime_list = data.get("realtimeArrivalList", [])
                res = {}
                for item in realtime_list:
                    subway_nm = str(item.get("subwayNm", "")).replace(" ", "")
                    msg = item.get("arvlMsg2")
                    if subway_nm and msg:
                        if subway_nm not in res: res[subway_nm] = []
                        res[subway_nm].append(msg)
                
                # 중복 제거 및 상위 2개 시간만 유지
                for k in res: res[k] = list(dict.fromkeys(res[k]))[:2]
                return station_name, res
    except Exception: pass
    return station_name, {}

# =====================================================================
# 공공데이터(TAGO) 버스 정류장 및 실시간 정보 조회 (변경 없음)
# =====================================================================
async def get_tago_nodes(lat: float, lon: float, session: aiohttp.ClientSession) -> list:
    if not TAGO_API_KEY: return []
    url = "http://apis.data.go.kr/1613000/BusSttnInfoInqireService/getCrdntPrxmtSttnList"
    params = {"serviceKey": TAGO_API_KEY.strip(), "gpsLati": str(lat), "gpsLong": str(lon), "_type": "json", "numOfRows": "5", "pageNo": "1"}
    try:
        async with session.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=3.0) as response:
            if response.status != 200: return []
            data = await response.json()
            items = data.get("response", {}).get("body", {}).get("items", {}).get("item", [])
            if isinstance(items, dict): items = [items]
            return [(str(it.get("citycode", "")), str(it.get("nodeid", ""))) for it in items if it.get("citycode") and it.get("nodeid")]
    except Exception: return []

async def fetch_tago_bus_arrivals(node_id: str, city_code: str, session: aiohttp.ClientSession) -> dict:
    if not TAGO_API_KEY: return {}
    url = "http://apis.data.go.kr/1613000/ArvlInfoInqireService/getSttnAcctoArvlPrearngeInfoList"
    params = {"serviceKey": TAGO_API_KEY.strip(), "cityCode": str(city_code), "nodeId": str(node_id), "_type": "json", "numOfRows": "20", "pageNo": "1"}
    try:
        async with session.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=3.0) as response: 
            if response.status != 200: return {}
            data = await response.json()
            items = data.get("response", {}).get("body", {}).get("items", {}).get("item", [])
            if isinstance(items, dict): items = [items]
            
            bus_info = {}
            for item in items:
                route_no = str(item.get("routeno"))
                arr_time_min = item.get("arrtime", 0) // 60
                if route_no not in bus_info: bus_info[route_no] = []
                bus_info[route_no].append(arr_time_min)
                
            result = {}
            for bus, times in bus_info.items():
                times.sort()
                result[bus] = [f"{t}분 후" if t > 0 else "곧 도착" for t in times][:2]
            return result
    except Exception: return {}

async def fetch_and_cache(lat_str: str, lon_str: str, session: aiohttp.ClientSession, sem: asyncio.Semaphore):
    async with sem:
        nodes = await get_tago_nodes(float(lat_str), float(lon_str), session)
        
    merged_bus_info = {}
    if nodes:
        async def bounded_fetch(node, city):
            async with sem:
                return await fetch_tago_bus_arrivals(node, city, session)
                
        tasks = [bounded_fetch(node, city) for city, node in nodes]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for res in results:
            if isinstance(res, dict):
                for bus_no, times in res.items():
                    if bus_no not in merged_bus_info: merged_bus_info[bus_no] = times
                    else: merged_bus_info[bus_no] = list(dict.fromkeys(merged_bus_info[bus_no] + times))[:2]
    return f"{lat_str}_{lon_str}", merged_bus_info

# =====================================================================
# 🌟 [최적화 2] TMAP 로직에서 실시간 조회를 완전히 제거. '순수 탐색'만 진행.
# =====================================================================
async def get_coords_from_tmap(place_name: str) -> tuple[float, float]:
    url = "https://apis.openapi.sk.com/tmap/pois"
    headers = {"appKey": TMAP_API_KEY, "accept": "application/json"}
    params = {"version": "1", "searchKeyword": place_name, "count": "1"}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, params=params) as response:
            if response.status == 200:
                data = await response.json()
                if "searchPoiInfo" in data and "pois" in data["searchPoiInfo"]:
                    poi = data["searchPoiInfo"]["pois"]["poi"][0]
                    return float(poi["noorLon"]), float(poi["noorLat"])
    raise HTTPException(status_code=400, detail=f"'{place_name}' 장소를 찾을 수 없습니다.")

async def fetch_segments_from_tmap(start: LocationPoint, end: LocationPoint, opt_type: str, search_date: str = None) -> list[RouteResponse]:
    if start.latitude == end.latitude and start.longitude == end.longitude:
        return [RouteResponse(totalTimeMin=0, totalFareWon=0, totalWalkDistanceMeter=0, segments=[RouteSegment(segmentType="WALK", instruction="도보 이동", durationMin=0, startLocationName=start.name, endLocationName=end.name, pathCoordinates=[], transitOptions=[])])]

    url = "https://apis.openapi.sk.com/transit/routes"
    headers = {"appKey": TMAP_API_KEY, "accept": "application/json", "content-type": "application/json"}
    payload = {"startX": str(start.longitude), "startY": str(start.latitude), "endX": str(end.longitude), "endY": str(end.latitude), "count": 10, "lang": 0, "format": "json"}
    if search_date: payload["searchDttm"] = search_date

    async with aiohttp.ClientSession() as session: 
        async with session.post(url, headers=headers, json=payload) as response:
            if response.status != 200: raise HTTPException(status_code=500, detail="TMAP 연동 실패")
            data = await response.json()
            try:
                itineraries = data.get("metaData", {}).get("plan", {}).get("itineraries", [])
                if not itineraries: raise HTTPException(status_code=400, detail="경로가 없습니다.")

                if opt_type == "MIN_TIME": itineraries.sort(key=lambda x: x.get("totalTime", 999999))
                elif opt_type == "MIN_COST": itineraries.sort(key=lambda x: x.get("fare", {}).get("regular", {}).get("totalFare", 999999))
                elif opt_type == "MIN_TRANSFER": itineraries.sort(key=lambda x: x.get("transferCount", 99))
                elif opt_type == "MIN_WALK": itineraries.sort(key=lambda x: x.get("totalWalkDistance", 999999))

                def parse_linestring(ls_str: str) -> list[Coordinate]:
                    coords = []
                    if not ls_str: return coords
                    for pt in ls_str.strip().split():
                        parts = pt.split(',')
                        if len(parts) >= 2:
                            try: coords.append(Coordinate(latitude=float(parts[1]), longitude=float(parts[0])))
                            except ValueError: continue
                    return coords

                parsed_routes = []
                for path in itineraries[:10]:
                    total_time = path.get("totalTime", 0) // 60
                    total_fare = path.get("fare", {}).get("regular", {}).get("totalFare", 0)
                    total_walk = path.get("totalWalkDistance", 0)
                    segments = []
                    
                    for leg in path.get("legs", []):
                        mode = leg.get("mode", "WALK")
                        if "BUS" in mode: mode = "BUS"
                        elif mode == "TRAIN": mode = "SUBWAY"
                            
                        section_time = leg.get("sectionTime", 0) // 60
                        start_name = leg.get("start", {}).get("name", "출발")
                        end_name = leg.get("end", {}).get("name", "도착")
                        
                        path_coords = []
                        transit_options = []
                        
                        if mode == "WALK":
                            instruction = "도보 이동"
                            for step in leg.get("steps", []):
                                ls = step.get("linestring", "") or step.get("lineString", "")
                                path_coords.extend(parse_linestring(ls))
                        else:
                            route_name = leg.get("route", "대중교통")
                            route_names = [r.strip() for r in route_name.split(",")]
                            
                            s_lat, s_lon = leg.get("start", {}).get("lat"), leg.get("start", {}).get("lon")
                            if not s_lat or not s_lon:
                                pass_stops = leg.get("passStopList", {}).get("stationList", [])
                                if pass_stops:
                                    s_lat, s_lon = pass_stops[0].get("lat", s_lat), pass_stops[0].get("lon", s_lon)
                            
                            # 🌟 [핵심] TMAP 탐색 중에는 무조건 '시간표 참조'로 세팅 (시간 단축)
                            for r_name in route_names:
                                transit_options.append(TransitOption(routeName=r_name, arrivalTime1="시간표 참조", arrivalTime2=None))
                            
                            if len(route_names) > 1: instruction = f"[{route_names[0]}] 외 {len(route_names)-1}대 승차 ➔ {end_name} 하차"
                            else: instruction = f"[{route_names[0]}] 승차 ➔ {end_name} 하차"

                            pass_shape = leg.get("passShape")
                            ls = pass_shape if isinstance(pass_shape, str) else (pass_shape.get("linestring", "") if pass_shape else "")
                            path_coords.extend(parse_linestring(ls))
                            
                            # 실시간 타겟팅을 위해 정확한 좌표를 맨 앞에 삽입
                            if s_lat and s_lon and not path_coords:
                                path_coords.insert(0, Coordinate(latitude=float(s_lat), longitude=float(s_lon)))
                            elif s_lat and s_lon:
                                path_coords[0] = Coordinate(latitude=float(s_lat), longitude=float(s_lon))

                        segments.append(RouteSegment(
                            segmentType=mode, instruction=instruction, durationMin=section_time,
                            startLocationName=start_name, endLocationName=end_name, pathCoordinates=path_coords,
                            transitOptions=transit_options
                        ))
                    
                    parsed_routes.append(RouteResponse(totalTimeMin=total_time, totalFareWon=total_fare, totalWalkDistanceMeter=total_walk, segments=segments))
                
                return parsed_routes
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"파싱 오류: {str(e)}")


# =====================================================================
# 🌟 [최적화 3] 메인 프로세스: 경로 선발 ➔ 타겟팅 실시간 데이터 주입
# =====================================================================
async def process_optimized_route(request: RouteRequest):
    all_points = [request.startPoint] + request.anchorPoints + [request.endPoint]
    
    # 1. 지오코딩 병렬 처리
    async def resolve_coords(point):
        if point.latitude == 0.0 and point.longitude == 0.0:
            lon, lat = await get_coords_from_tmap(point.name)
            point.longitude, point.latitude = lon, lat
            
    await asyncio.gather(*[resolve_coords(pt) for pt in all_points])

    # 2. 실시간 조회 없이 초고속 TMAP 경로 탐색
    segment_tasks = []
    for i in range(len(all_points) - 1):
        task = fetch_segments_from_tmap(
            start=all_points[i], end=all_points[i+1], 
            opt_type=request.optimizationType.value, search_date=request.searchDate
        )
        segment_tasks.append(task)

    legs_alternatives = await asyncio.gather(*segment_tasks)
    optimized_legs = [legs[:3] for legs in legs_alternatives]
    all_combinations = list(itertools.product(*optimized_legs))

    if request.optimizationType.value == "MIN_TIME": all_combinations.sort(key=lambda combo: sum(r.totalTimeMin for r in combo))
    elif request.optimizationType.value == "MIN_COST": all_combinations.sort(key=lambda combo: sum(r.totalFareWon for r in combo))
    elif request.optimizationType.value == "MIN_WALK": all_combinations.sort(key=lambda combo: sum(r.totalWalkDistanceMeter for r in combo))

    # 3. 상위 5개의 최종 경로만 확정
    final_routes = []
    for combo in all_combinations[:5]: # 🌟 무조건 상위 5개만!
        total_time, total_fare, total_walk, merged_segments = 0, 0, 0, []
        for idx, route in enumerate(combo):
            total_time += route.totalTimeMin
            total_fare += route.totalFareWon
            total_walk += route.totalWalkDistanceMeter
            merged_segments.extend(route.segments)
            
            if idx < len(combo) - 1:
                wait_point = all_points[idx + 1]
                merged_segments.append(RouteSegment(
                    segmentType="WAIT", instruction=f"[{wait_point.name}] 경유지", durationMin=5, 
                    startLocationName=wait_point.name, endLocationName=wait_point.name, 
                    pathCoordinates=[Coordinate(latitude=wait_point.latitude, longitude=wait_point.longitude)], transitOptions=[]
                ))
                total_time += 5

        final_routes.append(RouteResponse(
            totalTimeMin=total_time, totalFareWon=total_fare, totalWalkDistanceMeter=total_walk, segments=merged_segments,
            startCoordinate=Coordinate(latitude=all_points[0].latitude, longitude=all_points[0].longitude),
            endCoordinate=Coordinate(latitude=all_points[-1].latitude, longitude=all_points[-1].longitude)
        ))

    # =========================================================
    # 🌟 [최적화 핵심] 확정된 5개 경로의 '첫 번째 탑승 정류장'만 추출하여 타겟팅 조회
    # =========================================================
    unique_bus_coords = set()
    unique_subways = set()
    
    # 첫 번째 대중교통 탑승 위치(도보 제외)만 수집
    for r in final_routes:
        for seg in r.segments:
            if seg.segmentType == "BUS":
                if seg.pathCoordinates:
                    unique_bus_coords.add((str(seg.pathCoordinates[0].latitude), str(seg.pathCoordinates[0].longitude)))
                break # 첫 탑승구간만 찾으면 다음 경로로
            elif seg.segmentType in ["SUBWAY", "TRAIN"]:
                unique_subways.add(seg.startLocationName)
                break

    tago_data = {}
    subway_data = {}

    # 수집된 1~5개의 정류장만 병렬 조회 (Vercel 과부하 원천 차단)
    async with aiohttp.ClientSession() as session:
        sem = asyncio.Semaphore(5)
        tasks = [fetch_and_cache(lat, lon, session, sem) for lat, lon in unique_bus_coords]
        tasks += [fetch_seoul_subway_all(name, session) for name in unique_subways]
        
        if tasks:
            try:
                results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=4.0)
                for res in results:
                    if not isinstance(res, Exception):
                        k, v = res
                        if "_" in k: tago_data[k] = v
                        else: subway_data[k] = v
            except asyncio.TimeoutError:
                pass # 공공데이터 서버 장애 시 안전하게 시간표 상태로 통과

    # 4. 조회된 실시간 데이터를 최종 5개 경로에 주입 (Hydration)
    def match_subway(tmap_line, real_time_dict):
        t_line = tmap_line.replace("수도권", "").replace(" ", "")
        for api_line, times in real_time_dict.items():
            if t_line == api_line or t_line.replace("선", "") == api_line.replace("선", ""): return times
            elif ("수인" in t_line or "분당" in t_line) and "신분당" not in t_line:
                if ("수인" in api_line or "분당" in api_line) and "신분당" not in api_line: return times
            elif "신분당" in t_line and "신분당" in api_line: return times
            elif t_line in api_line or api_line in t_line: return times
        return []

    for r in final_routes:
        for seg in r.segments:
            if seg.segmentType == "BUS":
                if seg.pathCoordinates:
                    lat, lon = str(seg.pathCoordinates[0].latitude), str(seg.pathCoordinates[0].longitude)
                    real_time = tago_data.get(f"{lat}_{lon}", {})
                    
                    for opt in seg.transitOptions:
                        r_clean = re.sub(r'[가-힣\s\(\)]', '', opt.routeName)
                        for tago_bus_no, tago_times in real_time.items():
                            tago_clean = re.sub(r'[가-힣\s\(\)]', '', tago_bus_no)
                            if r_clean and tago_clean and (r_clean == tago_clean or r_clean in tago_clean or tago_clean in r_clean):
                                opt.arrivalTime1 = tago_times[0] if len(tago_times) > 0 else "시간표 참조"
                                opt.arrivalTime2 = tago_times[1] if len(tago_times) > 1 else None
                                break
                break # 첫 탑승구간만 실시간 조회 주입
                
            elif seg.segmentType in ["SUBWAY", "TRAIN"]:
                s_name = seg.startLocationName
                real_time = subway_data.get(s_name, {})
                for opt in seg.transitOptions:
                    times = match_subway(opt.routeName, real_time)
                    if times:
                        opt.arrivalTime1 = times[0] if len(times) > 0 else "시간표 참조"
                        opt.arrivalTime2 = times[1] if len(times) > 1 else None
                break

    return {"routes": final_routes}