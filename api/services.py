import os
import aiohttp
import traceback
from fastapi import HTTPException
from api.schemas import RouteRequest, RouteResponse, RouteSegment, LocationPoint, Coordinate

# Vercel 환경변수 충돌 방지
for key in ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY']:
    os.environ.pop(key, None)

# Vercel 환경변수에서 TMAP 키 가져오기
TMAP_API_KEY = os.environ.get("TMAP_API_KEY")

# 1. 지오코딩 (이름 -> 위경도 변환)
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
                    # X(경도, lon), Y(위도, lat) 순으로 반환
                    return float(poi["noorLon"]), float(poi["noorLat"])
                    
    raise HTTPException(status_code=400, detail=f"'{place_name}' 장소를 찾을 수 없습니다.")


# [Step 2 & 3] TMAP 대중교통 경로 탐색 및 데이터 파싱
async def fetch_segment_from_tmap(start: LocationPoint, end: LocationPoint, opt_type: str) -> RouteResponse:
    if start.latitude == end.latitude and start.longitude == end.longitude:
        return RouteResponse(
            totalTimeMin=0, totalFareWon=0, totalWalkDistanceMeter=0,
            segments=[RouteSegment(
                segmentType="WALK", instruction="도보 이동", durationMin=0,
                startLocationName=start.name, endLocationName=end.name, pathCoordinates=[]
            )]
        )

    url = "https://apis.openapi.sk.com/transit/routes"
    headers = {
        "appKey": TMAP_API_KEY,
        "accept": "application/json",
        "content-type": "application/json"
    }
    
    # [핵심] TMAP 서버에 요청할 때 X=longitude(경도), Y=latitude(위도) 맵핑 원칙 엄수
    payload = {
        "startX": str(start.longitude),
        "startY": str(start.latitude),
        "endX": str(end.longitude),
        "endY": str(end.latitude),
        "count": 10,
        "lang": 0,
        "format": "json"
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as response:
            if response.status != 200:
                error_text = await response.text()
                print(f"TMAP Transit API Error: {error_text}")
                raise HTTPException(status_code=500, detail="TMAP 대중교통 길찾기 연동에 실패했습니다.")
                
            data = await response.json()
            
            try:
                meta_data = data.get("metaData", {})
                plan = meta_data.get("plan", {})
                itineraries = plan.get("itineraries", [])
                
                if not itineraries:
                    raise HTTPException(status_code=400, detail="요청하신 구간의 대중교통 경로가 없습니다.")

                # 최적화 타입(opt_type)에 따른 경로 정렬 로직
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
                
                # [Step 4] LineString 전용 파서 (X, Y 완벽 분리)
                def parse_linestring(ls_str: str) -> list[Coordinate]:
                    coords = []
                    if not ls_str:
                        return coords
                    
                    # TMAP 형식: "127.1234,37.1234 127.1235,37.1235" -> 공백으로 분리
                    points = ls_str.strip().split()
                    for pt in points:
                        parts = pt.split(',')
                        if len(parts) >= 2:
                            try:
                                lon = float(parts[0]) # 첫번째는 X (경도)
                                lat = float(parts[1]) # 두번째는 Y (위도)
                                # 프론트엔드가 요구하는 형식에 맞춰 매핑
                                coords.append(Coordinate(latitude=lat, longitude=lon))
                            except ValueError:
                                continue
                    return coords

                for leg in legs:
                    # 이동 수단 매핑
                    mode = leg.get("mode", "WALK")
                    if mode in ["EXPRESSBUS", "INTERCITYBUS"]: 
                        mode = "BUS"
                    elif mode == "TRAIN":
                        mode = "SUBWAY"
                        
                    section_time = leg.get("sectionTime", 0) // 60
                    start_name = leg.get("start", {}).get("name", "출발")
                    end_name = leg.get("end", {}).get("name", "도착")
                    
                    path_coords = []
                    instruction = ""
                    
                    if mode == "WALK":
                        instruction = "도보 이동"
                        for step in leg.get("steps", []):
                            ls = step.get("linestring", "") or step.get("lineString", "")
                            path_coords.extend(parse_linestring(ls))
                    else:
                        route_name = leg.get("route", "대중교통")
                        instruction = f"[{route_name}] {start_name} 승차 -> {end_name} 하차"
                        
                        pass_shape = leg.get("passShape")
                        ls = pass_shape if isinstance(pass_shape, str) else (pass_shape.get("linestring", "") if pass_shape else "")
                        path_coords.extend(parse_linestring(ls))

                    # TMAP에서 예외적으로 선 좌표를 안줄 경우 시작점/도착점 직선으로 강제 연결
                    if not path_coords:
                        s_lat, s_lon = leg.get("start", {}).get("lat"), leg.get("start", {}).get("lon")
                        e_lat, e_lon = leg.get("end", {}).get("lat"), leg.get("end", {}).get("lon")
                        if s_lat and s_lon:
                            path_coords.append(Coordinate(latitude=float(s_lat), longitude=float(s_lon)))
                        if e_lat and e_lon:
                            path_coords.append(Coordinate(latitude=float(e_lat), longitude=float(e_lon)))

                    segments.append(RouteSegment(
                        segmentType=mode, instruction=instruction,
                        durationMin=section_time, startLocationName=start_name,
                        endLocationName=end_name, pathCoordinates=path_coords
                    ))
                
                return RouteResponse(
                    totalTimeMin=total_time, totalFareWon=total_fare, 
                    totalWalkDistanceMeter=total_walk, segments=segments
                )
            except Exception as e:
                # 500 에러 발생 시 로그에 남기기 위한 추적 코드
                traceback.print_exc()
                raise HTTPException(status_code=500, detail=f"TMAP 경로 데이터 파싱 오류: {str(e)}")


# [Step 5] 메인 루틴 연결 (지오코딩 + 경로 탐색 조합)
async def process_optimized_route(request: RouteRequest) -> RouteResponse:
    all_points = [request.startPoint] + request.anchorPoints + [request.endPoint]
    
    # 1. 텍스트 검색어들을 모두 TMAP 지오코딩으로 정확한 위경도로 셋팅
    for point in all_points:
        if point.latitude == 0.0 and point.longitude == 0.0:
            lon, lat = await get_coords_from_tmap(point.name)
            point.longitude = lon  # X (경도)
            point.latitude = lat   # Y (위도)

    total_time, total_fare, total_walk = 0, 0, 0
    merged_segments = []
    
    # 2. 좌표간 대중교통 길찾기 (새로 만든 TMAP 연동)
    for i in range(len(all_points) - 1):
        segment_response = await fetch_segment_from_tmap(
            start=all_points[i], 
            end=all_points[i+1], 
            opt_type=request.optimizationType.value
        )
        
        total_time += segment_response.totalTimeMin
        total_fare += segment_response.totalFareWon
        total_walk += segment_response.totalWalkDistanceMeter
        merged_segments.extend(segment_response.segments)
        
        # 경유지 체류 시간(5분) 강제 할당
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