import os
import aiohttp
import traceback
import itertools
import urllib.parse
import re
import asyncio  
import xml.etree.ElementTree as ET
from fastapi import HTTPException
from api.schemas import RouteRequest, RouteResponse, RouteSegment, LocationPoint, Coordinate, TransitOption

# 프록시 환경변수 제거
for key in ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY']:
    os.environ.pop(key, None)

TMAP_API_KEY = os.environ.get("TMAP_API_KEY")
# 발급받으신 1개의 통합 키로 서울, 경기, 전국 API를 모두 호출합니다.
TAGO_API_KEY = os.environ.get("TAGO_API_KEY") 
SEOUL_SUBWAY_API_KEY = os.environ.get("SEOUL_SUBWAY_API_KEY")

SUBWAY_ID_MAP = {
    "1001": "1호선", "1002": "2호선", "1003": "3호선", "1004": "4호선",
    "1005": "5호선", "1006": "6호선", "1007": "7호선", "1008": "8호선",
    "1009": "9호선", "1063": "경의중앙선", "1065": "공항철도", "1067": "경춘선",
    "1075": "수인분당선", "1077": "신분당선", "1092": "우이신설선", "1093": "서해선",
    "1081": "경강선", "1069": "인천1호선", "1071": "인천2호선", "1089": "신림선", "1032": "GTXA"
}

# =====================================================================
# 🚇 [지하철] 서울/수도권 지하철 실시간 도착 정보
# =====================================================================
async def fetch_seoul_subway_all(station_name: str, session: aiohttp.ClientSession) -> tuple:
    if not SEOUL_SUBWAY_API_KEY: return station_name, {}
    subway_key = SEOUL_SUBWAY_API_KEY.strip()
    clean_name = re.split(r'역|\(|\.|·', station_name)[0].strip()
    encoded_name = urllib.parse.quote(clean_name)
    url = f"http://swopenapi.seoul.go.kr/api/subway/{subway_key}/json/realtimeStationArrival/0/15/{encoded_name}"
    try:
        async with session.get(url, timeout=3.5) as response:
            if response.status == 200:
                data = await response.json()
                realtime_list = data.get("realtimeArrivalList", [])
                res = {}
                for item in realtime_list:
                    subway_id = str(item.get("subwayId", ""))
                    subway_nm = SUBWAY_ID_MAP.get(subway_id, "")
                    if not subway_nm: subway_nm = str(item.get("subwayNm", "")).replace(" ", "")
                    msg = item.get("arvlMsg2")
                    if subway_nm and msg:
                        if subway_nm not in res: res[subway_nm] = []
                        res[subway_nm].append(msg)
                for k in res: res[k] = list(dict.fromkeys(res[k]))[:2]
                return station_name, res
    except Exception: pass
    return station_name, {}

# =====================================================================
# 🚍 [스마트 라우터 1] 서울 시내버스 전용 탐색기
# =====================================================================
async def fetch_seoul_bus(lat, lon, session, key):
    try:
        url_pos = f"http://ws.bus.go.kr/api/rest/stationinfo/getStationByPos?ServiceKey={key}&tmX={lon}&tmY={lat}&radius=150&resultType=json"
        async with session.get(url_pos, timeout=2.5) as resp:
            data = await resp.json()
            items = data.get('msgBody', {}).get('itemList', [])
            if not items: return {}
            ars_ids = [str(it['arsId']) for it in items[:2] if str(it.get('arsId', '0')) != '0']

        res = {}
        for ars_id in ars_ids:
            url_arr = f"http://ws.bus.go.kr/api/rest/stationinfo/getStationByUid?ServiceKey={key}&arsId={ars_id}&resultType=json"
            async with session.get(url_arr, timeout=2.5) as resp:
                arr_data = await resp.json()
                for it in arr_data.get('msgBody', {}).get('itemList', []):
                    rtNm = it.get('rtNm')
                    msg1 = it.get('arrmsg1')
                    msg2 = it.get('arrmsg2')
                    if rtNm and msg1 and "종료" not in msg1:
                        if rtNm not in res: res[rtNm] = []
                        res[rtNm].append(msg1)
                        if msg2 and "종료" not in msg2: res[rtNm].append(msg2)
        return res
    except Exception: return {}

# =====================================================================
# 🚍 [스마트 라우터 2] 경기도 시내/마을버스 전용 탐색기
# =====================================================================
async def fetch_gyeonggi_bus(lat, lon, session, key):
    try:
        url_pos = f"http://apis.data.go.kr/6410000/busstationservice/getBusStationAroundList?serviceKey={key}&x={lon}&y={lat}"
        async with session.get(url_pos, timeout=2.5) as resp:
            root = ET.fromstring(await resp.text())
            station_ids = [elem.text for elem in root.findall('.//stationId')][:3]

        route_ids = set()
        arrival_data = []
        for st_id in station_ids:
            url_arr = f"http://apis.data.go.kr/6410000/busarrivalservice/getBusArrivalList?serviceKey={key}&stationId={st_id}"
            async with session.get(url_arr, timeout=2.5) as resp:
                arr_root = ET.fromstring(await resp.text())
                for item in arr_root.findall('.//busArrivalList'):
                    rid = item.findtext('routeId')
                    t1 = item.findtext('predictTime1')
                    t2 = item.findtext('predictTime2')
                    if rid and t1 and t1 != '0':
                        route_ids.add(rid)
                        arrival_data.append((rid, t1, t2))

        route_map = {}
        # 노선 ID를 실제 버스 이름(예: 6-1)으로 번역
        async def resolve_route(rid):
            try:
                u = f"http://apis.data.go.kr/6410000/busrouteservice/getBusRouteInfoItem?serviceKey={key}&routeId={rid}"
                async with session.get(u, timeout=2.0) as r_resp:
                    r_root = ET.fromstring(await r_resp.text())
                    return rid, r_root.findtext('.//routeName')
            except Exception: return rid, None

        r_results = await asyncio.gather(*[resolve_route(r) for r in route_ids], return_exceptions=True)
        for res in r_results:
            if not isinstance(res, Exception):
                rid, rname = res
                if rname: route_map[rid] = rname

        res = {}
        for rid, t1, t2 in arrival_data:
            rname = route_map.get(rid)
            if rname:
                if rname not in res: res[rname] = []
                res[rname].append(f"{t1}분 후")
                if t2 and t2 != '0': res[rname].append(f"{t2}분 후")
        return res
    except Exception: return {}

# =====================================================================
# 🚍 [스마트 라우터 3] 전국구(TAGO) 범용 탐색기 (기존 유지)
# =====================================================================
async def fetch_tago_bus(lat, lon, session, key):
    try:
        url = f"http://apis.data.go.kr/1613000/BusSttnInfoInqireService/getCrdntPrxmtSttnList?serviceKey={key}&gpsLati={lat}&gpsLong={lon}&_type=json&numOfRows=5&pageNo=1"
        async with session.get(url, timeout=2.5) as resp:
            data = await resp.json()
            items = data.get("response", {}).get("body", {}).get("items", {}).get("item", [])
            if isinstance(items, dict): items = [items]
            nodes = [(str(it.get("citycode", "")), str(it.get("nodeid", ""))) for it in items if it.get("citycode") and it.get("nodeid")]

        res = {}
        for city, node in nodes[:3]:
            url_arr = f"http://apis.data.go.kr/1613000/ArvlInfoInqireService/getSttnAcctoArvlPrearngeInfoList?serviceKey={key}&cityCode={city}&nodeId={node}&_type=json&numOfRows=15&pageNo=1"
            async with session.get(url_arr, timeout=2.5) as arr_resp:
                arr_data = await arr_resp.json()
                arr_items = arr_data.get("response", {}).get("body", {}).get("items", {}).get("item", [])
                if isinstance(arr_items, dict): arr_items = [arr_items]
                for item in arr_items:
                    route_no = str(item.get("routeno"))
                    arr_time = item.get("arrtime", 0) // 60
                    if route_no not in res: res[route_no] = []
                    res[route_no].append(arr_time)

        final_res = {}
        for bus, times in res.items():
            times.sort()
            final_res[bus] = [f"{t}분 후" if t > 0 else "곧 도착" for t in times][:2]
        return final_res
    except Exception: return {}

# =====================================================================
# 🚀 3대 라우터 동시 발사 및 데이터 병합
# =====================================================================
async def fetch_all_bus_arrivals(lat_str, lon_str, session, sem):
    if not TAGO_API_KEY: return f"{lat_str}_{lon_str}", {}
    key = TAGO_API_KEY.strip()
    
    async with sem:
        lat, lon = float(lat_str), float(lon_str)
        # 서울, 경기, 전국 API를 동시에 호출합니다.
        tasks = [
            fetch_seoul_bus(lat, lon, session, key),
            fetch_gyeonggi_bus(lat, lon, session, key),
            fetch_tago_bus(lat, lon, session, key)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        merged_bus_info = {}
        for res in results:
            if isinstance(res, dict):
                for bus, times in res.items():
                    if bus not in merged_bus_info:
                        merged_bus_info[bus] = times
                    else:
                        merged_bus_info[bus] = list(dict.fromkeys(merged_bus_info[bus] + times))[:2]
        return f"{lat_str}_{lon_str}", merged_bus_info

# =====================================================================
# TMAP 로직 (실시간 조회를 뺀 초고속 순수 탐색)
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
                            
                            pass_stops = leg.get("passStopList", {}).get("stationList", [])
                            if pass_stops:
                                s_lat = pass_stops[0].get("lat", s_lat)
                                s_lon = pass_stops[0].get("lon", s_lon)
                            
                            for r_name in route_names:
                                transit_options.append(TransitOption(routeName=r_name, arrivalTime1="시간표 참조", arrivalTime2=None))
                            
                            if len(route_names) > 1: instruction = f"[{route_names[0]}] 외 {len(route_names)-1}대 승차 ➔ {end_name} 하차"
                            else: instruction = f"[{route_names[0]}] 승차 ➔ {end_name} 하차"

                            pass_shape = leg.get("passShape")
                            ls = pass_shape if isinstance(pass_shape, str) else (pass_shape.get("linestring", "") if pass_shape else "")
                            path_coords.extend(parse_linestring(ls))
                            
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
# 메인 프로세스: 경로 선발 ➔ 타겟팅 실시간 데이터 주입
# =====================================================================
async def process_optimized_route(request: RouteRequest):
    all_points = [request.startPoint] + request.anchorPoints + [request.endPoint]
    
    async def resolve_coords(point):
        if point.latitude == 0.0 and point.longitude == 0.0:
            lon, lat = await get_coords_from_tmap(point.name)
            point.longitude, point.latitude = lon, lat
            
    await asyncio.gather(*[resolve_coords(pt) for pt in all_points])

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

    final_routes = []
    for combo in all_combinations[:5]: 
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

    unique_bus_coords = set()
    unique_subways = set()
    
    for r in final_routes:
        for seg in r.segments:
            if seg.segmentType == "BUS":
                if seg.pathCoordinates:
                    unique_bus_coords.add((str(seg.pathCoordinates[0].latitude), str(seg.pathCoordinates[0].longitude)))
                break 
            elif seg.segmentType in ["SUBWAY", "TRAIN"]:
                unique_subways.add(seg.startLocationName)
                break

    bus_data = {}
    subway_data = {}

    async with aiohttp.ClientSession() as session:
        sem = asyncio.Semaphore(15) 
        tasks = [fetch_all_bus_arrivals(lat, lon, session, sem) for lat, lon in unique_bus_coords]
        tasks += [fetch_seoul_subway_all(name, session) for name in unique_subways]
        
        if tasks:
            try:
                # 🌟 [넉넉한 대기시간] 3개의 서버를 동시 호출하므로 시간을 조금 더 확보합니다.
                results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=7.5)
                for res in results:
                    if not isinstance(res, Exception):
                        k, v = res
                        if "_" in k: bus_data[k] = v
                        else: subway_data[k] = v
            except asyncio.TimeoutError:
                pass 

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
                    real_time = bus_data.get(f"{lat}_{lon}", {})
                    
                    for opt in seg.transitOptions:
                        r_clean = re.sub(r'[^a-zA-Z0-9\-]', '', opt.routeName).lstrip('0')
                        for api_bus_no, api_times in real_time.items():
                            api_clean = re.sub(r'[^a-zA-Z0-9\-]', '', api_bus_no).lstrip('0')
                            
                            if r_clean and api_clean and (r_clean == api_clean or r_clean in api_clean or api_clean in r_clean):
                                opt.arrivalTime1 = api_times[0] if len(api_times) > 0 else "시간표 참조"
                                opt.arrivalTime2 = api_times[1] if len(api_times) > 1 else None
                                break
                break 
                
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