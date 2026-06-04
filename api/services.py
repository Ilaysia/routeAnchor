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

# services.py 내부 함수 수정

# =====================================================================
# 서울/수도권 지하철 실시간 도착 정보 조회
# =====================================================================
async def fetch_seoul_subway_arrivals(station_name: str, target_line: str) -> list:
    if not SEOUL_SUBWAY_API_KEY: return []
    
    subway_key = SEOUL_SUBWAY_API_KEY.strip()
    clean_name = re.split(r'역|\(|\.|·', station_name)[0].strip()
    encoded_name = urllib.parse.quote(clean_name)
    
    url = f"http://swopenapi.seoul.go.kr/api/subway/{subway_key}/json/realtimeStationArrival/0/10/{encoded_name}"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=3.0) as response:
                if response.status == 200:
                    data = await response.json()
                    times = []
                    
                    for item in data.get("realtimeArrivalList", []):
                        # 서울 API의 호선 이름 추출 (공백 제거)
                        subway_nm = str(item.get("subwayNm", "")).replace(" ", "")
                        t_line = target_line.replace("수도권", "").replace(" ", "")
                        
                        is_match = False
                        
                        # 🌟 [수정 포인트] 지하철 이름이 확실히 존재할 때만 엄격하게 매칭합니다.
                        if subway_nm:
                            # 1. 완벽히 일치하거나 '선'을 제외하고 일치할 때
                            if t_line == subway_nm or t_line.replace("선", "") == subway_nm.replace("선", ""):
                                is_match = True
                            # 2. 수인분당선 예외 처리 (신분당선과 혼동 방지)
                            elif ("수인" in t_line or "분당" in t_line) and "신분당" not in t_line:
                                if ("수인" in subway_nm or "분당" in subway_nm) and "신분당" not in subway_nm:
                                    is_match = True
                            # 3. 신분당선 처리
                            elif "신분당" in t_line and "신분당" in subway_nm:
                                is_match = True
                            # 4. 그 외 "1호선"이 "수도권1호선"에 포함되는 등 범용 매칭
                            elif t_line in subway_nm or subway_nm in t_line:
                                is_match = True
                                
                        if is_match:
                            msg = item.get("arvlMsg2")
                            if msg:
                                times.append(msg)
                                
                    return list(dict.fromkeys(times))[:2]
    except Exception: 
        pass
    return []
# =====================================================================
# [Step 1] 주변 정류장 3개 정보 가져오기
# =====================================================================
async def get_tago_nodes(lat: float, lon: float, session: aiohttp.ClientSession) -> list[tuple[str, str]]:
    if not TAGO_API_KEY: return []
    
    url = "http://apis.data.go.kr/1613000/BusSttnInfoInqireService/getCrdntPrxmtSttnList"
    params = {
        "serviceKey": TAGO_API_KEY.strip(),
        "gpsLati": str(lat),
        "gpsLong": str(lon),
        "_type": "json",
        "numOfRows": "3",  
        "pageNo": "1"
    }
    headers = {"User-Agent": "Mozilla/5.0"}
    
    try:
        async with session.get(url, params=params, headers=headers, timeout=3.5) as response:
            if response.status != 200: return []
            try:
                data = await response.json()
            except Exception:
                return []
            
            items = data.get("response", {}).get("body", {}).get("items", {}).get("item", [])
            if isinstance(items, dict): items = [items]
            
            nodes = []
            for it in items:
                city = str(it.get("citycode", ""))
                node = str(it.get("nodeid", ""))
                if city and node: nodes.append((city, node))
            return nodes
    except Exception:
        return []

# =====================================================================
# [Step 2] 버스 도착 정보 가져오기 
# =====================================================================
async def fetch_tago_bus_arrivals(node_id: str, city_code: str, session: aiohttp.ClientSession) -> dict:
    if not TAGO_API_KEY: return {}
    
    url = "http://apis.data.go.kr/1613000/ArvlInfoInqireService/getSttnAcctoArvlPrearngeInfoList"
    params = {
        "serviceKey": TAGO_API_KEY.strip(),
        "cityCode": str(city_code),
        "nodeId": str(node_id),
        "_type": "json",
        "numOfRows": "20",
        "pageNo": "1"
    }
    headers = {"User-Agent": "Mozilla/5.0"}
    
    try:
        async with session.get(url, params=params, headers=headers, timeout=3.5) as response: 
            if response.status != 200: return {}
            try:
                data = await response.json()
            except Exception:
                return {}
            
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
                result[bus] = [f"{t}분" if t > 0 else "곧 도착" for t in times][:2]
            return result
    except Exception:
        return {}

# =====================================================================
# [Step 3] 병렬 처리 + 신호등 제어 (DDoS 오인 방지)
# =====================================================================
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
                    if bus_no not in merged_bus_info:
                        merged_bus_info[bus_no] = times
                    else:
                        all_times = merged_bus_info[bus_no] + times
                        seen = set()
                        unique_times = []
                        for t in all_times:
                            if t not in seen:
                                seen.add(t)
                                unique_times.append(t)
                        merged_bus_info[bus_no] = unique_times[:2]
    return f"{lat_str}_{lon_str}", merged_bus_info

# =====================================================================
# TMAP 지오코딩 및 길찾기 핵심 로직
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

# 🌟 [변경] is_first_segment 파라미터 추가
async def fetch_segments_from_tmap(start: LocationPoint, end: LocationPoint, opt_type: str, search_date: str = None, is_first_segment: bool = False) -> list[RouteResponse]:
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

                unique_bus_coords = set()
                
                # 첫 번째 구간일 때만 버스 정류장 좌표를 추출합니다.
                if is_first_segment:
                    for path in itineraries[:3]:
                        for leg in path.get("legs", []):
                            mode = leg.get("mode", "WALK")
                            if "BUS" in mode:
                                s_lat, s_lon = leg.get("start", {}).get("lat"), leg.get("start", {}).get("lon")
                                if not leg.get("start", {}).get("stationId"):
                                    pass_stops = leg.get("passStopList", {}).get("stationList", [])
                                    if pass_stops:
                                        s_lat, s_lon = pass_stops[0].get("lat", s_lat), pass_stops[0].get("lon", s_lon)
                                if s_lat and s_lon:
                                    unique_bus_coords.add((str(s_lat), str(s_lon)))

                tago_cache = {}
                # 🌟 [트래픽 감소] 첫 번째 구간(is_first_segment)일 때만 실시간 버스 정보를 호출합니다.
                if is_first_segment and unique_bus_coords:
                    sem = asyncio.Semaphore(3)
                    tasks = [fetch_and_cache(lat, lon, session, sem) for lat, lon in unique_bus_coords]
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for res in results:
                        if not isinstance(res, Exception):
                            k, v = res
                            tago_cache[k] = v

                parsed_routes = []
                for idx, path in enumerate(itineraries[:10]):
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
                            
                            station_id = leg.get("start", {}).get("stationId", "")
                            s_lat, s_lon = leg.get("start", {}).get("lat"), leg.get("start", {}).get("lon")

                            if not station_id:
                                pass_stops = leg.get("passStopList", {}).get("stationList", [])
                                if pass_stops:
                                    station_id = pass_stops[0].get("stationID", "")
                                    s_lat, s_lon = pass_stops[0].get("lat", s_lat), pass_stops[0].get("lon", s_lon)
                            
                            if mode == "BUS" and s_lat and s_lon:
                                # 🌟 첫 번째 구간 + 상위 3개 경로에만 실시간 데이터를 넣습니다.
                                if is_first_segment and idx < 3:
                                    cache_key = f"{str(s_lat)}_{str(s_lon)}"
                                    real_time_data = tago_cache.get(cache_key, {})
                                    
                                    for r_name in route_names:
                                        times = []
                                        r_clean = re.sub(r'[^a-zA-Z0-9\-]', '', r_name)
                                        for tago_bus_no, tago_times in real_time_data.items():
                                            tago_clean = re.sub(r'[^a-zA-Z0-9\-]', '', tago_bus_no)
                                            if r_clean and tago_clean and r_clean == tago_clean:
                                                times = tago_times
                                                break
                                                
                                        arr1 = times[0] if len(times) > 0 else "정보 없음"
                                        arr2 = times[1] if len(times) > 1 else None
                                        transit_options.append(TransitOption(routeName=r_name, arrivalTime1=arr1, arrivalTime2=arr2))
                                else:
                                    # 첫 번째 구간이 아니거나 하위 경로면 무조건 시간표 참조
                                    for r_name in route_names:
                                        transit_options.append(TransitOption(routeName=r_name, arrivalTime1="시간표 참조", arrivalTime2=None))
                            
                            elif mode == "SUBWAY":
                                if is_first_segment and idx < 3:
                                    for r_name in route_names:
                                        times = await fetch_seoul_subway_arrivals(start_name, r_name)
                                        arr1 = times[0] if len(times) > 0 else "시간표 참조"
                                        arr2 = times[1] if len(times) > 1 else None
                                        transit_options.append(TransitOption(routeName=r_name, arrivalTime1=arr1, arrivalTime2=arr2))
                                else:
                                    for r_name in route_names:
                                        transit_options.append(TransitOption(routeName=r_name, arrivalTime1="시간표 참조", arrivalTime2=None))
                            
                            else:
                                for r_name in route_names:
                                    transit_options.append(TransitOption(routeName=r_name, arrivalTime1="시간표 참조", arrivalTime2=None))
                            
                            if len(route_names) > 1: instruction = f"[{route_names[0]}] 외 {len(route_names)-1}대 승차 ➔ {end_name} 하차"
                            else: instruction = f"[{route_names[0]}] 승차 ➔ {end_name} 하차"

                            pass_shape = leg.get("passShape")
                            ls = pass_shape if isinstance(pass_shape, str) else (pass_shape.get("linestring", "") if pass_shape else "")
                            path_coords.extend(parse_linestring(ls))

                        if not path_coords:
                            s_lat_fallback, s_lon_fallback = leg.get("start", {}).get("lat"), leg.get("start", {}).get("lon")
                            e_lat_fallback, e_lon_fallback = leg.get("end", {}).get("lat"), leg.get("end", {}).get("lon")
                            if s_lat_fallback and s_lon_fallback: path_coords.append(Coordinate(latitude=float(s_lat_fallback), longitude=float(s_lon_fallback)))
                            if e_lat_fallback and e_lon_fallback: path_coords.append(Coordinate(latitude=float(e_lat_fallback), longitude=float(e_lon_fallback)))

                        segments.append(RouteSegment(
                            segmentType=mode, instruction=instruction, durationMin=section_time,
                            startLocationName=start_name, endLocationName=end_name, pathCoordinates=path_coords,
                            transitOptions=transit_options
                        ))
                    
                    parsed_routes.append(RouteResponse(totalTimeMin=total_time, totalFareWon=total_fare, totalWalkDistanceMeter=total_walk, segments=segments))
                
                return parsed_routes
            except Exception as e:
                traceback.print_exc()
                raise HTTPException(status_code=500, detail=f"파싱 오류: {str(e)}")

## =====================================================================
# 🌟 [핵심 변경] 모든 지오코딩과 경로 탐색 병렬 처리 및 조합(itertools) 최적화
# =====================================================================
async def process_optimized_route(request: RouteRequest):
    all_points = [request.startPoint] + request.anchorPoints + [request.endPoint]
    
    # 1. 지오코딩 병렬 처리 (좌표가 없는 장소 이름들 한 번에 변환)
    async def resolve_coords(point):
        if point.latitude == 0.0 and point.longitude == 0.0:
            lon, lat = await get_coords_from_tmap(point.name)
            point.longitude, point.latitude = lon, lat
            
    resolve_tasks = [resolve_coords(pt) for pt in all_points]
    await asyncio.gather(*resolve_tasks)

    # 2. 경로 탐색 병렬 처리 (구간별로 한 번에 API 호출)
    segment_tasks = []
    for i in range(len(all_points) - 1):
        is_first = (i == 0) # 첫 번째 구간만 실시간 정보 플래그 True
        task = fetch_segments_from_tmap(
            start=all_points[i], 
            end=all_points[i+1], 
            opt_type=request.optimizationType.value, 
            search_date=request.searchDate,
            is_first_segment=is_first
        )
        segment_tasks.append(task)

    # Vercel 타임아웃 방어막: 여기서 모든 구간 데이터를 한 번에 가져옵니다.
    legs_alternatives = await asyncio.gather(*segment_tasks)

    # 🌟 [치명적 에러 해결 포인트: 데이터 다이어트]
    # 각 구간별로 TMAP이 내려준 10개의 경로를 모두 곱하면 10^N 승으로 메모리가 터집니다.
    # 이미 fetch_segments_from_tmap 안에서 정렬되어 있으므로, 상위 3개씩만 잘라내어 조합합니다.
    optimized_legs = [legs[:3] for legs in legs_alternatives]

    # 3. 경로 조합 및 정렬 로직
    # 최대 3 * 3 * 3... 수준으로 연산량이 극적으로 감소하여 서버 다운이 방지됩니다.
    all_combinations = list(itertools.product(*optimized_legs))

    if request.optimizationType.value == "MIN_TIME": all_combinations.sort(key=lambda combo: sum(r.totalTimeMin for r in combo))
    elif request.optimizationType.value == "MIN_COST": all_combinations.sort(key=lambda combo: sum(r.totalFareWon for r in combo))
    elif request.optimizationType.value == "MIN_WALK": all_combinations.sort(key=lambda combo: sum(r.totalWalkDistanceMeter for r in combo))

    final_routes = []
    for combo in all_combinations[:10]: # 최종적으로 유저에게 보여줄 상위 10개만 선정
        total_time, total_fare, total_walk, merged_segments = 0, 0, 0, []
        for idx, route in enumerate(combo):
            total_time += route.totalTimeMin
            total_fare += route.totalFareWon
            total_walk += route.totalWalkDistanceMeter
            merged_segments.extend(route.segments)
            
            # 다음 구간으로 넘어갈 때 '경유지 대기(WAIT)' 세그먼트를 추가합니다.
            if idx < len(combo) - 1:
                wait_point = all_points[idx + 1]
                merged_segments.append(RouteSegment(
                    segmentType="WAIT", instruction=f"[{wait_point.name}] 경유지", durationMin=5, 
                    startLocationName=wait_point.name, endLocationName=wait_point.name, 
                    pathCoordinates=[Coordinate(latitude=wait_point.latitude, longitude=wait_point.longitude)], 
                    transitOptions=[]
                ))
                total_time += 5

        final_routes.append(RouteResponse(
            totalTimeMin=total_time, totalFareWon=total_fare, totalWalkDistanceMeter=total_walk, segments=merged_segments,
            startCoordinate=Coordinate(latitude=all_points[0].latitude, longitude=all_points[0].longitude),
            endCoordinate=Coordinate(latitude=all_points[-1].latitude, longitude=all_points[-1].longitude)
        ))
        
    return {"routes": final_routes}