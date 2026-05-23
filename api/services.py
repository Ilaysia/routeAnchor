import os
import aiohttp
import traceback
import itertools # [추가] 다중 경로 조합을 위한 모듈
from fastapi import HTTPException
from api.schemas import RouteRequest, RouteResponse, RouteSegment, LocationPoint, Coordinate

for key in ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY']:
    os.environ.pop(key, None)

TMAP_API_KEY = os.environ.get("TMAP_API_KEY")

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

# [변경됨] 단일 RouteResponse가 아닌, RouteResponse들의 리스트를 반환합니다.
async def fetch_segments_from_tmap(start: LocationPoint, end: LocationPoint, opt_type: str) -> list[RouteResponse]:
    if start.latitude == end.latitude and start.longitude == end.longitude:
        return [RouteResponse(
            totalTimeMin=0, totalFareWon=0, totalWalkDistanceMeter=0,
            segments=[RouteSegment(
                segmentType="WALK", instruction="도보 이동", durationMin=0,
                startLocationName=start.name, endLocationName=end.name, pathCoordinates=[]
            )]
        )]

    url = "https://apis.openapi.sk.com/transit/routes"
    headers = {
        "appKey": TMAP_API_KEY,
        "accept": "application/json",
        "content-type": "application/json"
    }
    
    payload = {
        "startX": str(start.longitude), "startY": str(start.latitude),
        "endX": str(end.longitude), "endY": str(end.latitude),
        "count": 10, "lang": 0, "format": "json"
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as response:
            if response.status != 200:
                raise HTTPException(status_code=500, detail="TMAP 대중교통 길찾기 연동에 실패했습니다.")
                
            data = await response.json()
            
            try:
                itineraries = data.get("metaData", {}).get("plan", {}).get("itineraries", [])
                if not itineraries:
                    raise HTTPException(status_code=400, detail="요청하신 구간의 대중교통 경로가 없습니다.")

                # 최적화 타입 정렬
                if opt_type == "MIN_TIME": itineraries.sort(key=lambda x: x.get("totalTime", 999999))
                elif opt_type == "MIN_COST": itineraries.sort(key=lambda x: x.get("fare", {}).get("regular", {}).get("totalFare", 999999))
                elif opt_type == "MIN_TRANSFER": itineraries.sort(key=lambda x: x.get("transferCount", 99))
                elif opt_type == "MIN_WALK": itineraries.sort(key=lambda x: x.get("totalWalkDistance", 999999))

                parsed_routes = []
                
                def parse_linestring(ls_str: str) -> list[Coordinate]:
                    coords = []
                    if not ls_str: return coords
                    points = ls_str.strip().split()
                    for pt in points:
                        parts = pt.split(',')
                        if len(parts) >= 2:
                            try: coords.append(Coordinate(latitude=float(parts[1]), longitude=float(parts[0])))
                            except ValueError: continue
                    return coords

                # [변경됨] 첫 번째 요소(best_path)만 쓰지 않고 최대 10개까지 모두 순회하여 리스트에 담습니다.
                for path in itineraries[:10]:
                    total_time = path.get("totalTime", 0) // 60
                    total_fare = path.get("fare", {}).get("regular", {}).get("totalFare", 0)
                    total_walk = path.get("totalWalkDistance", 0)

                    segments = []
                    legs = path.get("legs", [])
                    
                    for leg in legs:
                        mode = leg.get("mode", "WALK")
                        if mode in ["EXPRESSBUS", "INTERCITYBUS"]: mode = "BUS"
                        elif mode == "TRAIN": mode = "SUBWAY"
                            
                        section_time = leg.get("sectionTime", 0) // 60
                        start_name = leg.get("start", {}).get("name", "출발")
                        end_name = leg.get("end", {}).get("name", "도착")
                        
                        path_coords = []
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

                        if not path_coords:
                            s_lat, s_lon = leg.get("start", {}).get("lat"), leg.get("start", {}).get("lon")
                            e_lat, e_lon = leg.get("end", {}).get("lat"), leg.get("end", {}).get("lon")
                            if s_lat and s_lon: path_coords.append(Coordinate(latitude=float(s_lat), longitude=float(s_lon)))
                            if e_lat and e_lon: path_coords.append(Coordinate(latitude=float(e_lat), longitude=float(e_lon)))

                        segments.append(RouteSegment(
                            segmentType=mode, instruction=instruction, durationMin=section_time,
                            startLocationName=start_name, endLocationName=end_name, pathCoordinates=path_coords
                        ))
                    
                    parsed_routes.append(RouteResponse(
                        totalTimeMin=total_time, totalFareWon=total_fare, 
                        totalWalkDistanceMeter=total_walk, segments=segments
                    ))
                
                return parsed_routes # 최대 10개의 경로 리스트 반환
            except Exception as e:
                traceback.print_exc()
                raise HTTPException(status_code=500, detail=f"TMAP 경로 데이터 파싱 오류: {str(e)}")


async def process_optimized_route(request: RouteRequest): # -> 반환 타입은 FastAPI가 자동으로 JSON 맵핑합니다.
    all_points = [request.startPoint] + request.anchorPoints + [request.endPoint]
    
    for point in all_points:
        if point.latitude == 0.0 and point.longitude == 0.0:
            lon, lat = await get_coords_from_tmap(point.name)
            point.longitude = lon
            point.latitude = lat

    # [변경됨] 각 구간(Leg)별로 여러 개의 경로(최대 10개)를 모두 받아옵니다.
    legs_alternatives = []
    for i in range(len(all_points) - 1):
        alts = await fetch_segments_from_tmap(
            start=all_points[i], 
            end=all_points[i+1], 
            opt_type=request.optimizationType.value
        )
        legs_alternatives.append(alts)

    # [변경됨] 각 구간의 경로들을 조합(Cartesian Product)하여 전체 여정의 경우의 수를 만듭니다.
    all_combinations = list(itertools.product(*legs_alternatives))

    # [변경됨] 조합된 전체 여정 리스트를 사용자가 요청한 최적화 옵션에 따라 다시 전체 정렬합니다.
    if request.optimizationType.value == "MIN_TIME":
        all_combinations.sort(key=lambda combo: sum(r.totalTimeMin for r in combo))
    elif request.optimizationType.value == "MIN_COST":
        all_combinations.sort(key=lambda combo: sum(r.totalFareWon for r in combo))
    elif request.optimizationType.value == "MIN_WALK":
        all_combinations.sort(key=lambda combo: sum(r.totalWalkDistanceMeter for r in combo))

    # 상위 10개의 최종 경로 조합만 선택
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

            # 구간과 구간 사이에 대기(WAIT) 세그먼트 추가 (경유지가 있을 경우)
            if idx < len(combo) - 1:
                wait_point = all_points[idx + 1]
                merged_segments.append(RouteSegment(
                    segmentType="WAIT", instruction=f"[{wait_point.name}] 경유지", durationMin=5,
                    startLocationName=wait_point.name, endLocationName=wait_point.name,
                    pathCoordinates=[Coordinate(latitude=wait_point.latitude, longitude=wait_point.longitude)]
                ))
                total_time += 5

        final_routes.append(RouteResponse(
            totalTimeMin=total_time, totalFareWon=total_fare, totalWalkDistanceMeter=total_walk, segments=merged_segments,
            startCoordinate=Coordinate(latitude=all_points[0].latitude, longitude=all_points[0].longitude),
            endCoordinate=Coordinate(latitude=all_points[-1].latitude, longitude=all_points[-1].longitude)
        ))

    # [중요] 안드로이드가 기대하는 RouteListResponse 형태인 {"routes": [ ... ]} 딕셔너리로 반환합니다.
    return {"routes": final_routes}