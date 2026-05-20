import os
import aiohttp
import traceback
from fastapi import HTTPException
from api.schemas import RouteRequest, RouteResponse, RouteSegment, LocationPoint, Coordinate

# Vercel 환경변수 충돌 방지
for key in ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY']:
    os.environ.pop(key, None)

TMAP_API_KEY = os.environ.get("TMAP_API_KEY")
ODSAY_API_KEY = os.environ.get("ODSAY_API_KEY") # Vercel 환경변수에 등록된 키

# 1. 지오코딩 (이름 -> 위경도 변환)은 정확도가 높은 TMAP을 유지합니다.
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

# 2. 길찾기 및 폴리라인 그래픽 데이터는 ODSAY를 사용하여 100% 정확한 좌표를 획득합니다.
async def fetch_segment_from_odsay(start: LocationPoint, end: LocationPoint, opt_type: str) -> RouteResponse:
    if start.latitude == end.latitude and start.longitude == end.longitude:
        return RouteResponse(
            totalTimeMin=0, totalFareWon=0, totalWalkDistanceMeter=0,
            segments=[RouteSegment(
                segmentType="WALK", instruction="도보 이동", durationMin=0,
                startLocationName=start.name, endLocationName=end.name, pathCoordinates=[]
            )]
        )

    url = "https://api.odsay.com/v1/api/searchPubTransPathT"
    
    # ODsay OPT 설정 (0: 최단시간, 2: 최소환승)
    odsay_opt = "2" if opt_type == "MIN_TRANSFER" else "0"

    params = {
        "apiKey": ODSAY_API_KEY,
        "SX": str(start.longitude),
        "SY": str(start.latitude),
        "EX": str(end.longitude),
        "EY": str(end.latitude),
        "OPT": odsay_opt
    }
    
    async with aiohttp.ClientSession() as session:
        # Step 2-1: ODSAY 최적 길찾기 호출
        async with session.get(url, params=params) as response:
            if response.status != 200:
                raise HTTPException(status_code=500, detail="ODSAY 길찾기 연동 실패")
                
            data = await response.json()
            if "error" in data:
                raise HTTPException(status_code=400, detail=f"경로 탐색 실패: {data['error'].get('msg', '알 수 없는 오류')}")
            if "result" not in data or not data["result"].get("path"):
                raise HTTPException(status_code=400, detail="요청하신 구간의 대중교통 경로가 없습니다.")

            best_path = data["result"]["path"][0]
            info = best_path["info"]
            
            total_time = info.get("totalTime", 0)
            total_fare = info.get("payment", 0)
            total_walk = info.get("totalWalk", 0)
            map_obj = info.get("mapObj")

            # Step 2-2: ODSAY 그래픽 노선 데이터(폴리라인) 호출
            load_lane_data = None
            if map_obj:
                lane_url = "https://api.odsay.com/v1/api/loadLane"
                lane_params = {
                    "apiKey": ODSAY_API_KEY,
                    "mapObject": "0:0@" + map_obj if not map_obj.startswith("0:0@") else map_obj
                }
                async with session.get(lane_url, params=lane_params) as lane_resp:
                    if lane_resp.status == 200:
                        lane_json = await lane_resp.json()
                        if "result" in lane_json:
                            load_lane_data = lane_json["result"]

            segments = []
            sub_paths = best_path.get("subPath", [])
            transit_index = 0

            # Step 2-3: 데이터 병합 및 폴리라인 맵핑
            for step in sub_paths:
                traffic_type = step.get("trafficType") # 1:지하철, 2:버스, 3:도보

                if traffic_type == 3:
                    distance = step.get("distance", 0)
                    section_time = step.get("sectionTime", 0)
                    # ODSAY 도보 구간은 폴리라인을 비워둡니다. (프론트엔드에서 자동으로 점선을 이어줍니다)
                    segments.append(RouteSegment(
                        segmentType="WALK",
                        instruction=f"도보 이동 ({distance}m)",
                        durationMin=section_time,
                        startLocationName="환승/도보",
                        endLocationName="",
                        pathCoordinates=[] 
                    ))
                elif traffic_type in (1, 2):
                    mode = "BUS" if traffic_type == 2 else "SUBWAY"
                    start_name = step.get("startName", "출발역")
                    end_name = step.get("endName", "도착역")
                    section_time = step.get("sectionTime", 0)

                    lane_list = step.get("lane", [])
                    if lane_list:
                        route_name = lane_list[0].get("busNo", lane_list[0].get("name", "대중교통"))
                        instruction = f"[{route_name}] {start_name} 승차 -> {end_name} 하차"
                    else:
                        instruction = f"{start_name} -> {end_name}"

                    path_coords = []
                    
                    # 미리 뽑아둔 그래픽 데이터에서 정확한 굴곡 좌표(graphPos)를 가져와 매칭합니다.
                    if load_lane_data and "lane" in load_lane_data:
                        if transit_index < len(load_lane_data["lane"]):
                            lane_info = load_lane_data["lane"][transit_index]
                            for section in lane_info.get("section", []):
                                for pos in section.get("graphPos", []):
                                    path_coords.append(Coordinate(latitude=pos["y"], longitude=pos["x"]))
                            transit_index += 1

                    # 그래픽 데이터가 누락된 예외 상황에서는 버스정류장 직선 좌표라도 꽂아줍니다.
                    if not path_coords:
                        s_lat, s_lon = step.get("startY"), step.get("startX")
                        e_lat, e_lon = step.get("endY"), step.get("endX")
                        if s_lat and s_lon and e_lat and e_lon:
                            path_coords = [
                                Coordinate(latitude=float(s_lat), longitude=float(s_lon)),
                                Coordinate(latitude=float(e_lat), longitude=float(e_lon))
                            ]

                    segments.append(RouteSegment(
                        segmentType=mode,
                        instruction=instruction,
                        durationMin=section_time,
                        startLocationName=start_name,
                        endLocationName=end_name,
                        pathCoordinates=path_coords
                    ))

            return RouteResponse(
                totalTimeMin=total_time, totalFareWon=total_fare, totalWalkDistanceMeter=total_walk, segments=segments
            )

async def process_optimized_route(request: RouteRequest) -> RouteResponse:
    all_points = [request.startPoint] + request.anchorPoints + [request.endPoint]
    
    # 1. 텍스트 검색어들을 모두 TMAP 지오코딩으로 정확한 위경도로 셋팅
    for point in all_points:
        if point.latitude == 0.0 and point.longitude == 0.0:
            lon, lat = await get_coords_from_tmap(point.name)
            point.longitude = lon
            point.latitude = lat

    total_time, total_fare, total_walk = 0, 0, 0
    merged_segments = []
    
    # 2. 좌표간 길찾기는 ODSAY를 이용
    for i in range(len(all_points) - 1):
        segment_response = await fetch_segment_from_odsay(start=all_points[i], end=all_points[i+1], opt_type=request.optimizationType.value)
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