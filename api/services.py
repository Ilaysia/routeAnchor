import os
import httpx
from fastapi import HTTPException
from api.schemas import RouteRequest, RouteResponse, RouteSegment, LocationPoint, Coordinate

KAKAO_REST_API_KEY = os.environ.get("KAKAO_REST_API_KEY")
ODSAY_API_KEY = os.environ.get("ODSAY_API_KEY")
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

async def get_tmap_pedestrian_path(start_x, start_y, end_x, end_y) -> list:
    if start_x == end_x and start_y == end_y:
        return [Coordinate(latitude=float(start_y), longitude=float(start_x)),
                Coordinate(latitude=float(end_y), longitude=float(end_x))]

    url = "https://apis.openapi.sk.com/tmap/routes/pedestrian?version=1&format=json"
    headers = {"appKey": TMAP_API_KEY}
    payload = {
        "startX": str(start_x), "startY": str(start_y),
        "endX": str(end_x), "endY": str(end_y),
        "reqCoordType": "WGS84GEO", "resCoordType": "WGS84GEO",
        "startName": "출발", "endName": "도착"
    }
    
    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(url, headers=headers, json=payload)
            coords = []
            if res.status_code == 200:
                data = res.json()
                for feature in data.get("features", []):
                    geom = feature.get("geometry", {})
                    gtype = geom.get("type")
                    c_list = geom.get("coordinates", [])
                    
                    if gtype == "Point":
                        coords.append(Coordinate(latitude=float(c_list[1]), longitude=float(c_list[0])))
                    elif gtype == "LineString":
                        for c in c_list:
                            coords.append(Coordinate(latitude=float(c[1]), longitude=float(c[0])))
            return coords
    except Exception as e:
        print(f"TMAP 보행자 API 통신 에러: {e}")
        return []

async def fetch_segment_from_odsay(start: LocationPoint, end: LocationPoint, opt_type: str) -> RouteResponse:
    url = "https://api.odsay.com/v1/api/searchPubTransPathT"
    params = {
        "apiKey": ODSAY_API_KEY,
        "SX": start.longitude, "SY": start.latitude,
        "EX": end.longitude, "EY": end.latitude,
        "SearchPathType": 0 
    }
    
    async with httpx.AsyncClient() as client:
        response = await client.get(url, params=params)
        data = response.json()
        if "error" in data:
            raise HTTPException(status_code=400, detail=f"경로 탐색 실패: {data['error'].get('msg', '알 수 없는 오류')}")

        try:
            all_paths = data["result"]["path"]
            
            if opt_type == "MIN_TIME":
                all_paths.sort(key=lambda x: x["info"].get("totalTime", 999))
            elif opt_type == "MIN_COST":
                all_paths.sort(key=lambda x: x["info"].get("payment", 99999))
            elif opt_type == "MIN_TRANSFER":
                all_paths.sort(key=lambda x: x["info"].get("transitCount", 99))
            elif opt_type == "MIN_WALK":
                all_paths.sort(key=lambda x: x["info"].get("totalWalk", 99999))

            best_path = all_paths[0]
            info = best_path["info"]
            
            map_obj = info.get("mapObj")
            graphic_lanes = []
            
            if map_obj:
                try:
                    lane_url = "https://api.odsay.com/v1/api/loadLane"
                    lane_params = {"apiKey": ODSAY_API_KEY, "mapObject": map_obj}
                    
                    lane_res = await client.get(lane_url, params=lane_params)
                    if lane_res.status_code == 200:
                        lane_data = lane_res.json()
                        if "result" in lane_data and "lane" in lane_data["result"]:
                            graphic_lanes = lane_data["result"]["lane"]
                except Exception as e:
                    print(f"ODsay loadLane 에러: {e}")
            
            total_time = info.get("totalTime", 0)
            total_fare = info.get("payment", 0)
            total_walk = info.get("totalWalk", 0)
            
            segments = []
            lane_index = 0
            
            for sub in best_path.get("subPath", []):
                traffic_type = sub.get("trafficType")
                station_id = None
                path_coords = []

                if traffic_type in (1, 2):
                    if lane_index < len(graphic_lanes):
                        try:
                            lane_info = graphic_lanes[lane_index]
                            for section in lane_info.get("section", []):
                                for pos in section.get("graphPos", []):
                                    if "x" in pos and "y" in pos:
                                        path_coords.append(Coordinate(latitude=float(pos["y"]), longitude=float(pos["x"])))
                        except Exception as e:
                            print(f"그래픽 좌표 파싱 에러: {e}")
                        lane_index += 1
                    
                    if not path_coords:
                        pass_stop_list = sub.get("passStopList")
                        if pass_stop_list and isinstance(pass_stop_list, dict):
                            for station in pass_stop_list.get("stations", []):
                                sx = station.get("x")
                                sy = station.get("y")
                                if sx and sy:
                                    path_coords.append(Coordinate(latitude=float(sy), longitude=float(sx)))
                    
                    if not path_coords:
                        sx = sub.get("startX")
                        sy = sub.get("startY")
                        ex = sub.get("endX")
                        ey = sub.get("endY")
                        if sx and sy and ex and ey:
                            path_coords.append(Coordinate(latitude=float(sy), longitude=float(sx)))
                            path_coords.append(Coordinate(latitude=float(ey), longitude=float(ex)))

                elif traffic_type == 3:
                    start_x = sub.get("startX")
                    start_y = sub.get("startY")
                    end_x = sub.get("endX")
                    end_y = sub.get("endY")
                    if start_x and start_y and end_x and end_y:
                        walk_coords = await get_tmap_pedestrian_path(start_x, start_y, end_x, end_y)
                        if walk_coords:
                            path_coords.extend(walk_coords)
                        else:
                            path_coords.append(Coordinate(latitude=float(start_y), longitude=float(start_x)))
                            path_coords.append(Coordinate(latitude=float(end_y), longitude=float(end_x)))
                else:
                    continue

                if traffic_type == 1:
                    seg_type = "SUBWAY"
                    instruction = f"[{sub['lane'][0]['name']}] {sub['startName']}역 승차 -> {sub['endName']}역 하차"
                    station_id = str(sub.get("startID", ""))
                elif traffic_type == 2:
                    seg_type = "BUS"
                    instruction = f"[{sub['lane'][0]['busNo']} 버스] {sub['startName']} 정류장 승차 -> {sub['endName']} 정류장 하차"
                    station_id = str(sub.get("startID", ""))
                elif traffic_type == 3:
                    seg_type = "WALK"
                    instruction = f"도보 이동 ({sub.get('distance', 0)}m)"
                    
                segments.append(RouteSegment(
                    segmentType=seg_type,
                    instruction=instruction,
                    durationMin=sub.get("sectionTime", 0),
                    startLocationName=sub.get("startName", "도보 출발"),
                    endLocationName=sub.get("endName", "도보 도착"),
                    stationId=station_id if station_id else None,
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
            print(f"데이터 파싱 에러: {e}")
            raise HTTPException(status_code=500, detail=f"API 응답 구조 파싱 실패: {e}")

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
        
        segment_response = await fetch_segment_from_odsay(
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