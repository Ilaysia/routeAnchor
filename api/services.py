import os
import httpx
from fastapi import HTTPException
from api.schemas import RouteRequest, RouteResponse, RouteSegment, LocationPoint, Coordinate

for key in ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY']:
    os.environ.pop(key, None)

# 카카오는 이제 안 쓰므로 TMAP 키만 가져옵니다.
TMAP_API_KEY = os.environ.get("TMAP_API_KEY")

# [핵심 해결 로직] 카카오 지오코딩 대신 TMAP 지오코딩 API를 사용하여 좌표계를 100% TMAP으로 통일합니다.
async def get_coords_from_tmap(place_name: str) -> tuple[float, float]:
    url = "https://apis.openapi.sk.com/tmap/pois"
    headers = {
        "appKey": TMAP_API_KEY,
        "accept": "application/json"
    }
    params = {
        "version": "1",
        "searchKeyword": place_name,
        "count": 1
    }
    
    async with httpx.AsyncClient(trust_env=False) as client:
        response = await client.get(url, headers=headers, params=params)
        if response.status_code == 200:
            data = response.json()
            if "searchPoiInfo" in data and "pois" in data["searchPoiInfo"]:
                poi = data["searchPoiInfo"]["pois"]["poi"][0]
                lat = float(poi["noorLat"])
                lon = float(poi["noorLon"])
                return lon, lat
                
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
            raise HTTPException(status_code=500, detail="TMAP 연동 실패")
            
        data = response.json()
        try:
            itineraries = data["metaData"]["plan"]["itineraries"]
            
            if opt_type == "MIN_TIME": itineraries.sort(key=lambda x: x.get("totalTime", 999999))
            elif opt_type == "MIN_COST": itineraries.sort(key=lambda x: x.get("fare", {}).get("regular", {}).get("totalFare", 999999))
            elif opt_type == "MIN_TRANSFER": itineraries.sort(key=lambda x: x.get("transferCount", 99))
            elif opt_type == "MIN_WALK": itineraries.sort(key=lambda x: x.get("totalWalkDistance", 999999))

            best_path = itineraries[0]
            total_time = best_path.get("totalTime", 0) // 60
            total_fare = best_path.get("fare", {}).get("regular", {}).get("totalFare", 0)
            total_walk = best_path.get("totalWalkDistance", 0)
            
            segments = []
            legs = best_path.get("legs", [])
            
            def parse_linestring(ls_str: str) -> list:
                if not ls_str: return []
                pts = ls_str.replace(",", " ").split()
                coords = []
                for j in range(0, len(pts)-1, 2):
                    try:
                        coords.append(Coordinate(latitude=float(pts[j+1]), longitude=float(pts[j])))
                    except ValueError: pass
                return coords
            
            for leg in legs:
                mode = leg.get("mode", "WALK")
                if mode == "EXPRESSBUS": mode = "BUS"
                
                distance = leg.get("distance", 0)
                section_time = leg.get("sectionTime", 0) // 60
                start_name = leg.get("start", {}).get("name", "출발")
                end_name = leg.get("end", {}).get("name", "도착")
                
                path_coords = []
                if mode == "WALK":
                    for step in leg.get("steps", []):
                        ls = step.get("linestring", "") or step.get("lineString", "")
                        path_coords.extend(parse_linestring(ls))
                if mode == "BUS":
                    accurate_start = await get_accurate_stop_coords(start_name)
                    if accurate_start:
                        path_coords[0] = accurate_start # 경로의 첫 좌표를 카카오 정류장 좌표로 교체
                        
                else:
                    pass_shape = leg.get("passShape")
                    ls = pass_shape if isinstance(pass_shape, str) else (pass_shape.get("linestring", "") if pass_shape else "")
                    path_coords.extend(parse_linestring(ls))

                if not path_coords:
                    path_coords.append(Coordinate(latitude=start.latitude, longitude=start.longitude))
                    path_coords.append(Coordinate(latitude=end.latitude, longitude=end.longitude))

                segments.append(RouteSegment(
                    segmentType=mode, instruction=f"{start_name} -> {end_name}",
                    durationMin=section_time, startLocationName=start_name, endLocationName=end_name,
                    pathCoordinates=path_coords
                ))
            
            return RouteResponse(totalTimeMin=total_time, totalFareWon=total_fare, totalWalkDistanceMeter=total_walk, segments=segments)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"파싱 실패: {e}")
        
async def get_accurate_stop_coords(stop_name: str) -> Coordinate:
    url = "https://dapi.kakao.com/v2/local/search/keyword.json"
    headers = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}
    params = {"query": f"{stop_name} 버스정류장"} # 검색어에 '버스정류장'을 붙여 정확도 향상
    
    async with httpx.AsyncClient(trust_env=False) as client:
        response = await client.get(url, headers=headers, params=params)
        if response.status_code == 200:
            data = response.json()
            if data.get("documents"):
                # 카카오가 제공하는 가장 정확한 정류장 좌표 반환
                return Coordinate(latitude=float(data["documents"][0]["y"]), 
                                 longitude=float(data["documents"][0]["x"]))
    return None # 못 찾으면 TMAP 좌표 그대로 사용

async def process_optimized_route(request: RouteRequest) -> RouteResponse:
    all_points = [request.startPoint] + request.anchorPoints + [request.endPoint]
    for point in all_points:
        if point.latitude == 0.0 and point.longitude == 0.0:
            # [핵심 수정] 여기서 카카오 대신 방금 만든 get_coords_from_tmap을 호출합니다.
            lon, lat = await get_coords_from_tmap(point.name)
            point.longitude = lon
            point.latitude = lat

    total_time, total_fare, total_walk = 0, 0, 0
    merged_segments = []
    
    for i in range(len(all_points) - 1):
        segment_response = await fetch_segment_from_tmap(start=all_points[i], end=all_points[i+1], opt_type=request.optimizationType.value)
        total_time += segment_response.totalTimeMin
        total_fare += segment_response.totalFareWon
        total_walk += segment_response.totalWalkDistanceMeter
        merged_segments.extend(segment_response.segments)
        
        if i < len(all_points) - 2:
            merged_segments.append(RouteSegment(
                segmentType="WAIT", instruction=f"[{all_points[i+1].name}] 경유지", durationMin=5,
                startLocationName=all_points[i+1].name, endLocationName=all_points[i+1].name,
                pathCoordinates=[Coordinate(latitude=all_points[i+1].latitude, longitude=all_points[i+1].longitude)]
            ))
            total_time += 5

    return RouteResponse(
        totalTimeMin=total_time, totalFareWon=total_fare, totalWalkDistanceMeter=total_walk, segments=merged_segments,
        startCoordinate=Coordinate(latitude=all_points[0].latitude, longitude=all_points[0].longitude),
        endCoordinate=Coordinate(latitude=all_points[-1].latitude, longitude=all_points[-1].longitude)
    )