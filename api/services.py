import os
import httpx
import traceback
from fastapi import HTTPException
from api.schemas import RouteRequest, RouteResponse, RouteSegment, LocationPoint, Coordinate

for key in ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY']:
    os.environ.pop(key, None)

KAKAO_REST_API_KEY = os.environ.get("KAKAO_REST_API_KEY")
TMAP_API_KEY = os.environ.get("TMAP_API_KEY")

def slice_path_to_pin(coords: list, target_lat: float, target_lon: float, is_start: bool) -> list:
    if not coords: return coords
    
    min_dist = float('inf')
    closest_idx = 0
    for idx, pt in enumerate(coords):
        dist = (pt.latitude - target_lat)**2 + (pt.longitude - target_lon)**2
        if dist < min_dist:
            min_dist = dist
            closest_idx = idx
            
    if is_start:
        trimmed = coords[closest_idx:]
        if trimmed:
            trimmed[0] = Coordinate(latitude=target_lat, longitude=target_lon)
        return trimmed
    else:
        trimmed = coords[:closest_idx+1]
        if trimmed:
            trimmed[-1] = Coordinate(latitude=target_lat, longitude=target_lon)
        return trimmed

async def get_coords_from_kakao(place_name: str) -> tuple[float, float]:
    url = "[https://dapi.kakao.com/v2/local/search/keyword.json](https://dapi.kakao.com/v2/local/search/keyword.json)"
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

    url = "[https://apis.openapi.sk.com/transit/routes](https://apis.openapi.sk.com/transit/routes)"
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
            print(f"TMAP API 에러: {response.status_code} - {response.text}")
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
                        station_list = leg.get("passStopList", {}).get("stationList", [])
                        if station_list:
                            s_lat = float(station_list[0].get("lat", 0))
                            s_lon = float(station_list[0].get("lon", 0))
                            e_lat = float(station_list[-1].get("lat", 0))
                            e_lon = float(station_list[-1].get("lon", 0))
                            
                            if s_lat != 0 and s_lon != 0:
                                path_coords = slice_path_to_pin(path_coords, s_lat, s_lon, True)
                            if e_lat != 0 and e_lon != 0:
                                path_coords = slice_path_to_pin(path_coords, e_lat, e_lon, False)

                    if i == 0:
                        path_coords = slice_path_to_pin(path_coords, start.latitude, start.longitude, True)
                    if i == len(legs) - 1:
                        path_coords = slice_path_to_pin(path_coords, end.latitude, end.longitude, False)

                if not path_coords:
                    s_dict = leg.get("start") or {}
                    e_dict = leg.get("end") or {}
                    
                    s_lat = s_dict.get("lat")
                    s_lon = s_dict.get("lon")
                    e_lat = e_dict.get("lat")
                    e_lon = e_dict.get("lon")
                    
                    s_lat = float(s_lat) if s_lat else start.latitude
                    s_lon = float(s_lon) if s_lon else start.longitude
                    e_lat = float(e_lat) if e_lat else end.latitude
                    e_lon = float(e_lon) if e_lon else end.longitude
                    
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
                segments=segments
            )
            
        except Exception as e:
            err_msg = traceback.format_exc()
            print(f"CRITICAL PARSE ERROR: {err_msg}")
            raise HTTPException(status_code=500, detail=f"TMAP 데이터 파싱 에러: {str(e)}")

async def process_optimized_route(request: RouteRequest) -> RouteResponse:
    all_points = [request.startPoint] + request.anchorPoints + [request.endPoint]
    
    for point in all_points:
        if point.latitude == 0.0 and point.longitude == 0.0:
            lon, lat = await get_coords_from_kakao(point.name)
            point.longitude = lon
            point.latitude = lat
            print(f"[{point.name}] 좌표 변환 완료: 위도 {lat}, 경도 {lon}")

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