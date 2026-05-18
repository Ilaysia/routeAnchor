import os
import httpx
import traceback
from fastapi import HTTPException
from api.schemas import RouteRequest, RouteResponse, RouteSegment, LocationPoint, Coordinate

# Vercel 환경의 강제 프록시 변수 삭제 (500 에러 원천 차단)
for key in ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY']:
    os.environ.pop(key, None)

KAKAO_REST_API_KEY = os.environ.get("KAKAO_REST_API_KEY")
TMAP_API_KEY = os.environ.get("TMAP_API_KEY")

# 🚨 핵심: 버스/지하철 노선이 정류장 밖으로 튀어나가지 않게 실제 정류장 좌표 기준으로 잘라내는 함수
def trim_path(coords: list, start_lat: float, start_lon: float, end_lat: float, end_lon: float) -> list:
    if not coords: return coords
    
    min_start_dist = float('inf')
    start_idx = 0
    for idx, pt in enumerate(coords):
        dist = (pt.latitude - start_lat)**2 + (pt.longitude - start_lon)**2
        if dist < min_start_dist:
            min_start_dist = dist
            start_idx = idx
            
    min_end_dist = float('inf')
    end_idx = len(coords) - 1
    for idx, pt in enumerate(coords):
        dist = (pt.latitude - end_lat)**2 + (pt.longitude - end_lon)**2
        if dist < min_end_dist:
            min_end_dist = dist
            end_idx = idx
            
    if start_idx <= end_idx:
        trimmed = coords[start_idx:end_idx+1]
    else:
        trimmed = coords[end_idx:start_idx+1]
        trimmed.reverse()
        
    if trimmed:
        trimmed[0] = Coordinate(latitude=start_lat, longitude=start_lon)
        trimmed[-1] = Coordinate(latitude=end_lat, longitude=end_lon)
    return trimmed

async def get_coords_from_kakao(place_name: str) -> tuple[float, float]:
    url = "https://dapi.kakao.com/v2/local/search/keyword.json"
    headers = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}
    params = {"query": place_name}
    
    async with httpx.AsyncClient(trust_env=False) as client:
        response = await client.get(url, headers=headers, params=params)
        if response.status_code == 200:
            data = response.json()
            if data.get("documents"):
                x = float(data["documents"][0]["x"])
                y = float(data["documents"][0]["y"])
                return x, y
    raise HTTPException(status_code=400, detail=f"'{place_name}' 장소를 찾을 수 없습니다.")

async def fetch_segment_from_tmap(start: LocationPoint, end: LocationPoint, opt_type: str) -> RouteResponse:
    if start.latitude == end.latitude and start.longitude == end.longitude:
        return RouteResponse(
            totalTimeMin=0, totalFareWon=0, totalWalkDistanceMeter=0,
            segments=[RouteSegment(
                segmentType="WALK", instruction="도보 출발 및 도착", durationMin=0,
                startLocationName=start.name, endLocationName=end.name,
                pathCoordinates=[Coordinate(latitude=start.latitude, longitude=start.longitude)]
            )]
        )

    url = "https://apis.openapi.sk.com/transit/routes"
    headers = {
        "appKey": TMAP_API_KEY,
        "accept": "application/json",
        "content-type": "application/json"
    }
    
    payload = {
        "startX": str(start.longitude),
        "startY": str(start.latitude),
        "endX": str(end.longitude),
        "endY": str(end.latitude),
        "count": 10,
        "lang": 0,
        "format": "json"
    }
    
    async with httpx.AsyncClient(trust_env=False) as client:
        response = await client.post(url, headers=headers, json=payload)
        
        if response.status_code != 200:
            raise HTTPException(status_code=500, detail=f"TMAP 대중교통 API 통신 에러: HTTP {response.status_code}")
            
        data = response.json()
        
        try:
            itineraries = data["metaData"]["plan"]["itineraries"]
            
            if opt_type == "MIN_TIME":
                itineraries.sort(key=lambda x: x.get("totalTime", 999999))
            elif opt_type == "MIN_COST":
                itineraries.sort(key=lambda x: x.get("fare", {}).get("regular", {}).get("totalFare", 999999))
            elif opt_type == "MIN_TRANSFER":
                itineraries.sort(key=lambda x: x.get("transferCount", 99))
            elif opt_type == "MIN_WALK":
                itineraries.sort(key=lambda x: x.get("totalWalkDistance", 999999))

            best_path = itineraries[0]
            
            total_time = best_path.get("totalTime", 0) // 60
            total_fare = best_path.get("fare", {}).get("regular", {}).get("totalFare", 0)
            total_walk = best_path.get("totalWalkDistance", 0)
            
            segments = []
            legs = best_path.get("legs", [])
            
            def parse_linestring(ls_str: str) -> list:
                if not ls_str or not isinstance(ls_str, str): return []
                pts = ls_str.replace(",", " ").split()
                coords = []
                for j in range(0, len(pts)-1, 2):
                    try:
                        lon = float(pts[j])
                        lat = float(pts[j+1])
                        coords.append(Coordinate(latitude=lat, longitude=lon))
                    except ValueError:
                        pass
                return coords
            
            for i, leg in enumerate(legs):
                mode = leg.get("mode", "WALK")
                if mode == "EXPRESSBUS":
                    mode = "BUS"
                
                prev_mode = legs[i-1].get("mode") if i > 0 else None
                next_mode = legs[i+1].get("mode") if i < len(legs) - 1 else None
                
                is_adjacent_to_subway = (prev_mode == "SUBWAY" or next_mode == "SUBWAY")
                distance = leg.get("distance", 0)
                    
                section_time = leg.get("sectionTime", 0) // 60
                start_name = leg.get("start", {}).get("name", "출발")
                end_name = leg.get("end", {}).get("name", "도착")

                check_start_name = start.name if i == 0 else start_name
                check_end_name = end.name if i == len(legs) - 1 else end_name
                has_keyword = ("역" in check_start_name or "지하" in check_start_name or "역" in check_end_name or "지하" in check_end_name)
                
                path_coords = []
                
                if mode == "WALK":
                    if is_adjacent_to_subway or has_keyword or distance < 50:
                        s_dict = leg.get("start") or {}
                        e_dict = leg.get("end") or {}
                        start_lat = s_dict.get("lat")
                        start_lon = s_dict.get("lon")
                        end_lat = e_dict.get("lat")
                        end_lon = e_dict.get("lon")
                        
                        if start_lat and start_lon:
                            path_coords.append(Coordinate(latitude=float(start_lat), longitude=float(start_lon)))
                        if end_lat and end_lon:
                            path_coords.append(Coordinate(latitude=float(end_lat), longitude=float(end_lon)))
                    else:
                        for step in leg.get("steps", []):
                            ls = step.get("linestring", "") or step.get("lineString", "")
                            path_coords.extend(parse_linestring(ls))
                            
                    instruction = f"도보 이동 ({distance}m)"
                else:
                    pass_shape = leg.get("passShape")
                    ls = ""
                    if isinstance(pass_shape, dict):
                        ls = pass_shape.get("linestring", "") or pass_shape.get("lineString", "")
                    elif isinstance(pass_shape, str):
                        ls = pass_shape
                        
                    path_coords.extend(parse_linestring(ls))
                                
                    route_name = leg.get("route", "대중교통")
                    if mode == "BUS":
                        instruction = f"[{route_name} 버스] {start_name} 승차 -> {end_name} 하차"
                    elif mode == "SUBWAY":
                        instruction = f"[{route_name}] {start_name} 승차 -> {end_name} 하차"
                    else:
                        instruction = f"[{route_name}] {start_name} -> {end_name}"

                if path_coords:
                    if mode in ["BUS", "SUBWAY"]:
                        # 대중교통 노선이 정류장을 튀어나가지 않게 실제 정류장(passStopList) 기준으로 정확히 자름
                        station_list = leg.get("passStopList", {}).get("stationList", [])
                        if station_list:
                            s_lat = float(station_list[0].get("lat", 0))
                            s_lon = float(station_list[0].get("lon", 0))
                            e_lat = float(station_list[-1].get("lat", 0))
                            e_lon = float(station_list[-1].get("lon", 0))
                            
                            if s_lat != 0 and s_lon != 0 and e_lat != 0 and e_lon != 0:
                                path_coords = trim_path(path_coords, s_lat, s_lon, e_lat, e_lon)
                    else:
                        # 도보 구간도 앞뒤 버스/지하철과 완벽히 물리게 강제 동기화
                        s_dict = leg.get("start") or {}
                        e_dict = leg.get("end") or {}
                        s_lat = s_dict.get("lat")
                        s_lon = s_dict.get("lon")
                        e_lat = e_dict.get("lat")
                        e_lon = e_dict.get("lon")
                        if s_lat and s_lon and e_lat and e_lon:
                            path_coords[0] = Coordinate(latitude=float(s_lat), longitude=float(s_lon))
                            path_coords[-1] = Coordinate(latitude=float(e_lat), longitude=float(e_lon))

                if not path_coords:
                    s_dict = leg.get("start") or {}
                    e_dict = leg.get("end") or {}
                    s_lat = float(s_dict.get("lat")) if s_dict.get("lat") else start.latitude
                    s_lon = float(s_dict.get("lon")) if s_dict.get("lon") else start.longitude
                    e_lat = float(e_dict.get("lat")) if e_dict.get("lat") else end.latitude
                    e_lon = float(e_dict.get("lon")) if e_dict.get("lon") else end.longitude
                    path_coords.append(Coordinate(latitude=s_lat, longitude=s_lon))
                    path_coords.append(Coordinate(latitude=e_lat, longitude=e_lon))

                segments.append(RouteSegment(
                    segmentType=mode,
                    instruction=instruction,
                    durationMin=section_time,
                    startLocationName=start_name,
                    endLocationName=end_name,
                    stationId=None,
                    realTimeArrivalInfo=None, 
                    pathCoordinates=path_coords
                ))
            
            return RouteResponse(
                totalTimeMin=total_time,
                totalFareWon=total_fare,
                totalWalkDistanceMeter=total_walk,
                segments=segments,
                startCoordinate=Coordinate(latitude=start.latitude, longitude=start.longitude),
                endCoordinate=Coordinate(latitude=end.latitude, longitude=end.longitude)
            )
            
        except Exception as e:
            err_msg = traceback.format_exc()
            print(f"CRITICAL PARSE ERROR: {err_msg}")
            raise HTTPException(status_code=500, detail=f"TMAP 데이터 구조 파싱 실패: {e}")

async def process_optimized_route(request: RouteRequest) -> RouteResponse:
    all_points = [request.startPoint] + request.anchorPoints + [request.endPoint]
    
    for point in all_points:
        if point.latitude == 0.0 and point.longitude == 0.0:
            lon, lat = await get_coords_from_kakao(point.name)
            point.longitude = lon
            point.latitude = lat

    total_time = 0
    total_fare = 0
    total_walk = 0
    merged_segments = []
    
    for i in range(len(all_points) - 1):
        current_start = all_points[i]
        current_end = all_points[i+1]
        
        segment_response = await fetch_segment_from_tmap(
            start=current_start,
            end=current_end,
            opt_type=request.optimizationType.value
        )
        
        total_time += segment_response.totalTimeMin
        total_fare += segment_response.totalFareWon
        total_walk += segment_response.totalWalkDistanceMeter
        merged_segments.extend(segment_response.segments)
        
        if i < len(all_points) - 2:
            merged_segments.append(RouteSegment(
                segmentType="WAIT",
                instruction=f"[{current_end.name}] 앵커포인트 통과 및 환승 대기",
                durationMin=5,
                startLocationName=current_end.name,
                endLocationName=current_end.name,
                stationId=None,
                realTimeArrivalInfo=None,
                pathCoordinates=None
            ))
            total_time += 5

    anchor_coords = [Coordinate(latitude=p.latitude, longitude=p.longitude) for p in request.anchorPoints]
    return RouteResponse(
        totalTimeMin=total_time,
        totalFareWon=total_fare,
        totalWalkDistanceMeter=total_walk,
        segments=merged_segments,
        startCoordinate=Coordinate(latitude=all_points[0].latitude, longitude=all_points[0].longitude),
        endCoordinate=Coordinate(latitude=all_points[-1].latitude, longitude=all_points[-1].longitude),
        anchorCoordinates=anchor_coords
    )