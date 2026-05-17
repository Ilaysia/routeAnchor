import os
import httpx
from fastapi import HTTPException
from api.schemas import RouteRequest, RouteResponse, RouteSegment, LocationPoint, Coordinate

KAKAO_REST_API_KEY = os.environ.get("KAKAO_REST_API_KEY")
TMAP_API_KEY = os.environ.get("TMAP_API_KEY")

async def get_coords_from_kakao(place_name: str) -> tuple[float, float]:
    url = "https://dapi.kakao.com/v2/local/search/keyword.json"
    headers = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}
    params = {"query": place_name}
    
    async with httpx.AsyncClient() as client:
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
    
    async with httpx.AsyncClient() as client:
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
                        start_lat = leg.get("start", {}).get("lat")
                        start_lon = leg.get("start", {}).get("lon")
                        end_lat = leg.get("end", {}).get("lat")
                        end_lon = leg.get("end", {}).get("lon")
                        
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
                        # 대중교통 노선 꼬리 자르기: 진짜 정류장(stationList) 데이터 추출
                        station_list = leg.get("passStopList", {}).get("stationList", [])
                        if station_list:
                            first_station = station_list[0]
                            last_station = station_list[-1]
                            
                            real_start_lat = first_station.get("lat")
                            real_start_lon = first_station.get("lon")
                            real_end_lat = last_station.get("lat")
                            real_end_lon = last_station.get("lon")
                            
                            if real_start_lat and real_start_lon:
                                path_coords[0] = Coordinate(latitude=float(real_start_lat), longitude=float(real_start_lon))
                            if real_end_lat and real_end_lon:
                                path_coords[-1] = Coordinate(latitude=float(real_end_lat), longitude=float(real_end_lon))
                    else:
                        # 도보 구간 꼬리 자르기
                        leg_start_lat = leg.get("start", {}).get("lat")
                        leg_start_lon = leg.get("start", {}).get("lon")
                        leg_end_lat = leg.get("end", {}).get("lat")
                        leg_end_lon = leg.get("end", {}).get("lon")
                        
                        if leg_start_lat and leg_start_lon:
                            path_coords[0] = Coordinate(latitude=float(leg_start_lat), longitude=float(leg_start_lon))
                        if leg_end_lat and leg_end_lon:
                            path_coords[-1] = Coordinate(latitude=float(leg_end_lat), longitude=float(leg_end_lon))

                    # 전체 탐색의 진짜 처음과 끝은 카카오 핀 좌표로 덮어씌움 (단, 도보일 때만)
                    if i == 0 and mode == "WALK":
                        path_coords[0] = Coordinate(latitude=start.latitude, longitude=start.longitude)
                    if i == len(legs) - 1 and mode == "WALK":
                        path_coords[-1] = Coordinate(latitude=end.latitude, longitude=end.longitude)

                if not path_coords:
                    path_coords.append(Coordinate(latitude=start.latitude, longitude=start.longitude))
                    path_coords.append(Coordinate(latitude=end.latitude, longitude=end.longitude))

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
            print(f"TMAP 파싱 에러 상세: {e}, 응답 원본: {data}")
            raise HTTPException(status_code=500, detail=f"TMAP 데이터 구조 파싱 실패: {e}")

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