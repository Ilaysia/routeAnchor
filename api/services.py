import os
import aiohttp
import traceback
import itertools
from fastapi import HTTPException
from api.schemas import RouteRequest, RouteResponse, RouteSegment, LocationPoint, Coordinate, TransitOption

for key in ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY']:
    os.environ.pop(key, None)

TMAP_API_KEY = os.environ.get("TMAP_API_KEY")
TAGO_API_KEY = os.environ.get("TAGO_API_KEY")
SEOUL_SUBWAY_API_KEY = os.environ.get("SEOUL_SUBWAY_API_KEY", "sample") # 🌟 서울/수도권 지하철 API 키 추가

# =====================================================================
# [Step 1] TAGO 버스 지역코드 동적 획득
# =====================================================================
async def get_tago_city_code(lat: float, lon: float) -> str:
    if not TAGO_API_KEY: return "31190"
    url = f"http://apis.data.go.kr/1613000/BusSttnInfoInqireService/getCrdntPrxmtSttnList?serviceKey={TAGO_API_KEY}&gpsLati={lat}&gpsLong={lon}&_type=json&numOfRows=1&pageNo=1"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=2.0) as response:
                if response.status == 200:
                    data = await response.json()
                    items = data.get("response", {}).get("body", {}).get("items", {}).get("item", [])
                    if isinstance(items, dict): items = [items]
                    if items: return str(items[0].get("citycode", "31190"))
    except Exception as e: print(f"City Code 에러: {e}")
    return "31190"

# =====================================================================
# [Step 2] TAGO 버스 실시간 도착 정보 조회
# =====================================================================
async def fetch_seoul_subway_arrivals(station_name: str, target_line: str) -> list:
    if not SEOUL_SUBWAY_API_KEY or SEOUL_SUBWAY_API_KEY == "sample":
        print("경고: 서울 지하철 API 키가 등록되지 않아 'sample' 키로 작동 중입니다.")
        
    # 1. 역 이름 정제: "강남역(2호선)", "강남역", "강남" 모두 -> "강남" 으로 통일
    clean_name = re.split(r'역|\(', station_name)[0].strip()
    
    # 2. 한글 URL 인코딩 (필수: 한글이 깨져서 API 에러나는 현상 방지)
    encoded_name = urllib.parse.quote(clean_name)
    
    url = f"http://swopenapi.seoul.go.kr/api/subway/{SEOUL_SUBWAY_API_KEY}/json/realtimeStationArrival/0/10/{encoded_name}"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=3.0) as response:
                if response.status == 200:
                    data = await response.json()
                    times = []
                    
                    for item in data.get("realtimeArrivalList", []):
                        subway_line_name = item.get("subwayNm", "").replace(" ", "")
                        clean_target_line = target_line.replace(" ", "")
                        
                        # "수도권2호선" vs "2호선" 매칭 로직 고도화
                        if subway_line_name in clean_target_line or clean_target_line in subway_line_name:
                            # arvlMsg2 에 "5분 후", "강남 진입" 등의 진짜 정보가 들어있습니다.
                            times.append(item.get("arvlMsg2"))
                            
                    return list(dict.fromkeys(times))[:2]
    except Exception as e: 
        print(f"지하철 실시간 에러: {e}")
        
    return []

# =====================================================================
# [Step 3] 서울/수도권 지하철 실시간 도착 정보 조회
# =====================================================================
async def fetch_seoul_subway_arrivals(station_name: str, target_line: str) -> list:
    if not SEOUL_SUBWAY_API_KEY: return []
    if station_name.endswith("역"): station_name = station_name[:-1]
    
    url = f"http://swopenapi.seoul.go.kr/api/subway/{SEOUL_SUBWAY_API_KEY}/json/realtimeStationArrival/0/10/{station_name}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=3.0) as response:
                if response.status == 200:
                    data = await response.json()
                    times = []
                    for item in data.get("realtimeArrivalList", []):
                        subway_line_name = item.get("subwayNm", "")
                        # 지하철 호선 이름이 TMAP 데이터와 겹치는지 확인 (예: 2호선 == 서울 2호선)
                        if target_line[:2] in subway_line_name or subway_line_name[:2] in target_line:
                            times.append(item.get("arvlMsg2"))
                    return list(dict.fromkeys(times))[:2]
    except Exception as e: print(f"지하철 실시간 에러: {e}")
    return []

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

                parsed_routes = []
                def parse_linestring(ls_str: str) -> list[Coordinate]:
                    coords = []
                    if not ls_str: return coords
                    for pt in ls_str.strip().split():
                        parts = pt.split(',')
                        if len(parts) >= 2:
                            try: coords.append(Coordinate(latitude=float(parts[1]), longitude=float(parts[0])))
                            except ValueError: continue
                    return coords

                tago_cache = {}

                for path in itineraries[:10]:
                    total_time = path.get("totalTime", 0) // 60
                    total_fare = path.get("fare", {}).get("regular", {}).get("totalFare", 0)
                    total_walk = path.get("totalWalkDistance", 0)
                    segments = []
                    
                    for leg in path.get("legs", []):
                        mode = leg.get("mode", "WALK")
                        if mode in ["EXPRESSBUS", "INTERCITYBUS"]: mode = "BUS"
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
                            
                            # 🌟 1. 버스인 경우 (TAGO API 유연한 매칭)
                            if mode == "BUS" and station_id and s_lat and s_lon:
                                if station_id not in tago_cache:
                                    city_code = await get_tago_city_code(float(s_lat), float(s_lon))
                                    tago_cache[station_id] = await fetch_tago_bus_arrivals(station_id, city_code)
                                
                                real_time_data = tago_cache[station_id]
                                
                                # 🌟 [수정된 로직] 매칭 확인을 위한 로그 추가
                                print(f"DEBUG: 타야 할 버스들: {route_names}")
                                print(f"DEBUG: TAGO에서 받은 버스들: {list(real_time_data.keys())}")
                                
                                for r_name in route_names:
                                    times = []
                                    # 유연한 매칭: 번호가 포함되어 있는지 확인
                                    for tago_bus_no, tago_times in real_time_data.items():
                                        # 숫자만 추출해서 비교 (예: "66-4" -> "664")
                                        clean_r_name = "".join(filter(str.isdigit, r_name))
                                        clean_tago_no = "".join(filter(str.isdigit, tago_bus_no))
                                        
                                        if clean_r_name == clean_tago_no:
                                            times = tago_times
                                            break
                                            
                                    arr1 = times[0] if len(times) > 0 else "정보 없음"
                                    arr2 = times[1] if len(times) > 1 else None
                                    transit_options.append(TransitOption(routeName=r_name, arrivalTime1=arr1, arrivalTime2=arr2))
                                    
                            # 🌟 2. 지하철인 경우 (서울시 지하철 API 호출)
                            elif mode == "SUBWAY":
                                for r_name in route_names:
                                    times = await fetch_seoul_subway_arrivals(start_name, r_name)
                                    arr1 = times[0] if len(times) > 0 else "시간표 참조"
                                    arr2 = times[1] if len(times) > 1 else None
                                    transit_options.append(TransitOption(routeName=r_name, arrivalTime1=arr1, arrivalTime2=arr2))
                            
                            # 🌟 3. 그 외 (기차 등)
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

async def process_optimized_route(request: RouteRequest):
    all_points = [request.startPoint] + request.anchorPoints + [request.endPoint]
    for point in all_points:
        if point.latitude == 0.0 and point.longitude == 0.0:
            lon, lat = await get_coords_from_tmap(point.name)
            point.longitude, point.latitude = lon, lat

    legs_alternatives = []
    for i in range(len(all_points) - 1):
        alts = await fetch_segments_from_tmap(start=all_points[i], end=all_points[i+1], opt_type=request.optimizationType.value, search_date=request.searchDate)
        legs_alternatives.append(alts)

    all_combinations = list(itertools.product(*legs_alternatives))

    if request.optimizationType.value == "MIN_TIME": all_combinations.sort(key=lambda combo: sum(r.totalTimeMin for r in combo))
    elif request.optimizationType.value == "MIN_COST": all_combinations.sort(key=lambda combo: sum(r.totalFareWon for r in combo))
    elif request.optimizationType.value == "MIN_WALK": all_combinations.sort(key=lambda combo: sum(r.totalWalkDistanceMeter for r in combo))

    final_routes = []
    for combo in all_combinations[:10]:
        total_time, total_fare, total_walk, merged_segments = 0, 0, 0, []
        for idx, route in enumerate(combo):
            total_time += route.totalTimeMin
            total_fare += route.totalFareWon
            total_walk += route.totalWalkDistanceMeter
            merged_segments.extend(route.segments)
            if idx < len(combo) - 1:
                wait_point = all_points[idx + 1]
                merged_segments.append(RouteSegment(segmentType="WAIT", instruction=f"[{wait_point.name}] 경유지", durationMin=5, startLocationName=wait_point.name, endLocationName=wait_point.name, pathCoordinates=[Coordinate(latitude=wait_point.latitude, longitude=wait_point.longitude)], transitOptions=[]))
                total_time += 5

        final_routes.append(RouteResponse(
            totalTimeMin=total_time, totalFareWon=total_fare, totalWalkDistanceMeter=total_walk, segments=merged_segments,
            startCoordinate=Coordinate(latitude=all_points[0].latitude, longitude=all_points[0].longitude),
            endCoordinate=Coordinate(latitude=all_points[-1].latitude, longitude=all_points[-1].longitude)
        ))
    return {"routes": final_routes}