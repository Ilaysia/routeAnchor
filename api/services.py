import os
import aiohttp
import traceback
import itertools
from fastapi import HTTPException
from api.schemas import RouteRequest, RouteResponse, RouteSegment, LocationPoint, Coordinate, TransitOption

# Vercel 환경변수 충돌 방지
for key in ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY']:
    os.environ.pop(key, None)

TMAP_API_KEY = os.environ.get("TMAP_API_KEY")
TAGO_API_KEY = os.environ.get("TAGO_API_KEY")

# =====================================================================
# [Step 1] 좌표를 기반으로 해당 지역의 TAGO City Code 동적 추출
# =====================================================================
async def get_tago_city_code(lat: float, lon: float) -> str:
    if not TAGO_API_KEY:
        return "31190" # 키가 없을 시 기본값(용인)
        
    url = (f"http://apis.data.go.kr/1613000/BusSttnInfoInqireService/getCrdntPrxmtSttnList"
           f"?serviceKey={TAGO_API_KEY}&gpsLati={lat}&gpsLong={lon}&_type=json&numOfRows=1&pageNo=1")
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=2.0) as response:
                if response.status == 200:
                    data = await response.json()
                    items = data.get("response", {}).get("body", {}).get("items", {}).get("item", [])
                    if isinstance(items, dict):
                        items = [items]
                    if items:
                        return str(items[0].get("citycode", "31190"))
    except Exception as e:
        print(f"City Code 추출 에러: {e}")
    
    return "31190" # API 에러 시 안전하게 기본값 반환

# =====================================================================
# [Step 2] 추출된 City Code와 정류장 ID로 버스 실시간 정보 조회
# =====================================================================
async def fetch_tago_bus_arrivals(station_id: str, city_code: str) -> dict:
    if not TAGO_API_KEY or not station_id:
        return {}
    
    url = (f"http://apis.data.go.kr/1613000/BusSttnInfoInqireService/getSttnAcctoArvlPrearngeInfoList"
           f"?serviceKey={TAGO_API_KEY}&cityCode={city_code}&nodeId={station_id}&_type=json&numOfRows=50&pageNo=1")
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=3.0) as response: 
                if response.status == 200:
                    data = await response.json()
                    
                    items = data.get("response", {}).get("body", {}).get("items", {}).get("item", [])
                    if isinstance(items, dict):
                        items = [items]
                        
                    bus_info = {}
                    for item in items:
                        route_no = str(item.get("routeno"))
                        arr_time_sec = item.get("arrtime", 0)
                        arr_time_min = arr_time_sec // 60
                        
                        if route_no not in bus_info:
                            bus_info[route_no] = []
                        bus_info[route_no].append(arr_time_min)
                        
                    result = {}
                    for bus, times in bus_info.items():
                        times.sort()
                        formatted_times = [f"{t}분" if t > 0 else "곧 도착" for t in times]
                        result[bus] = formatted_times[:2]
                        
                    return result
    except Exception as e:
        print(f"TAGO 버스 정보 호출 에러: {e}")
    
    return {}

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
        return [RouteResponse(
            totalTimeMin=0, totalFareWon=0, totalWalkDistanceMeter=0,
            segments=[RouteSegment(segmentType="WALK", instruction="도보 이동", durationMin=0, startLocationName=start.name, endLocationName=end.name, pathCoordinates=[], transitOptions=[])]
        )]

    url = "https://apis.openapi.sk.com/transit/routes"
    headers = {"appKey": TMAP_API_KEY, "accept": "application/json", "content-type": "application/json"}
    
    payload = {
        "startX": str(start.longitude), "startY": str(start.latitude),
        "endX": str(end.longitude), "endY": str(end.latitude),
        "count": 10, "lang": 0, "format": "json"
    }
    if search_date:
        payload["searchDttm"] = search_date

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as response:
            if response.status != 200:
                raise HTTPException(status_code=500, detail="TMAP 대중교통 길찾기 연동에 실패했습니다.")
                
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

                # 동일 정류장 중복 API 호출 방지 캐시
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
                            s_lat = leg.get("start", {}).get("lat")
                            s_lon = leg.get("start", {}).get("lon")

                            if not station_id:
                                pass_stops = leg.get("passStopList", {}).get("stationList", [])
                                if pass_stops:
                                    station_id = pass_stops[0].get("stationID", "")
                                    s_lat = pass_stops[0].get("lat", s_lat)
                                    s_lon = pass_stops[0].get("lon", s_lon)
                            
                            # 🌟 동적 지역코드 및 버스 정보 호출 로직 적용
                            if mode == "BUS" and station_id and s_lat and s_lon:
                                if station_id not in tago_cache:
                                    # 1. 위/경도로 정확한 지역 코드 동적 획득
                                    city_code = await get_tago_city_code(float(s_lat), float(s_lon))
                                    # 2. 지역 코드 + 정류소 ID로 실시간 데이터 획득
                                    tago_cache[station_id] = await fetch_tago_bus_arrivals(station_id, city_code)
                                
                                real_time_data = tago_cache[station_id]
                                
                                for r_name in route_names:
                                    times = real_time_data.get(r_name, [])
                                    arr1 = times[0] if len(times) > 0 else "정보 없음"
                                    arr2 = times[1] if len(times) > 1 else None
                                    
                                    transit_options.append(TransitOption(routeName=r_name, arrivalTime1=arr1, arrivalTime2=arr2))
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
                raise HTTPException(status_code=500, detail=f"경로 데이터 파싱 오류: {str(e)}")

async def process_optimized_route(request: RouteRequest):
    all_points = [request.startPoint] + request.anchorPoints + [request.endPoint]
    
    for point in all_points:
        if point.latitude == 0.0 and point.longitude == 0.0:
            lon, lat = await get_coords_from_tmap(point.name)
            point.longitude = lon
            point.latitude = lat

    legs_alternatives = []
    for i in range(len(all_points) - 1):
        alts = await fetch_segments_from_tmap(
            start=all_points[i], end=all_points[i+1], opt_type=request.optimizationType.value, search_date=request.searchDate
        )
        legs_alternatives.append(alts)

    all_combinations = list(itertools.product(*legs_alternatives))

    if request.optimizationType.value == "MIN_TIME": all_combinations.sort(key=lambda combo: sum(r.totalTimeMin for r in combo))
    elif request.optimizationType.value == "MIN_COST": all_combinations.sort(key=lambda combo: sum(r.totalFareWon for r in combo))
    elif request.optimizationType.value == "MIN_WALK": all_combinations.sort(key=lambda combo: sum(r.totalWalkDistanceMeter for r in combo))

    top_10_combinations = all_combinations[:10]

    final_routes = []
    for combo in top_10_combinations:
        total_time, total_fare, total_walk = 0, 0, 0
        merged_segments = []

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