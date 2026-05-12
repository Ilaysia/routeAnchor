import os
import httpx
from fastapi import HTTPException
from schemas import RouteRequest, RouteResponse, RouteSegment, LocationPoint, Coordinate

# 이 아래의 함수나 로직들은 기존과 완전히 동일하게 유지하면 돼
KAKAO_REST_API_KEY = os.environ.get("KAKAO_REST_API_KEY")
ODSAY_API_KEY = os.environ.get("ODSAY_API_KEY")

async def get_coords_from_kakao(place_name: str) -> tuple[float, float]:
    url = "https://dapi.kakao.com/v2/local/search/keyword.json"
    headers = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}
    params = {"query": place_name}
    
    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers, params=params)
        
        print(f"--- 카카오 응답 [{place_name}]: {response.status_code} ---")
        if response.status_code != 200:
            print(f"카카오 에러 상세: {response.text}")
        
        data = response.json()
        if data.get("documents"):
            x = float(data["documents"][0]["x"])
            y = float(data["documents"][0]["y"])
            return x, y
                
    raise HTTPException(status_code=400, detail=f"'{place_name}' 장소를 찾을 수 없습니다.")

async def fetch_segment_from_odsay(start: LocationPoint, end: LocationPoint, opt_type: str) -> RouteResponse:
    url = "https://api.odsay.com/v1/api/searchPubTransPathT"
    params = {
        "apiKey": ODSAY_API_KEY,
        "SX": start.longitude,
        "SY": start.latitude,
        "EX": end.longitude,
        "EY": end.latitude,
        "SearchPathType": 0 
    }
    
    async with httpx.AsyncClient() as client:
        response = await client.get(url, params=params)
        
        print(f"--- ODsay 응답: {response.status_code} ---")
        data = response.json()
        if "error" in data:
            print(f"ODsay 에러 상세: {data['error']}")
            raise HTTPException(status_code=400, detail=f"경로 탐색 실패: {data['error'].get('msg', '알 수 없는 오류')}")
            
        if response.status_code != 200:
            raise HTTPException(status_code=500, detail="ODsay API 서버 에러")

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
            
            total_time = info.get("totalTime", 0)
            total_fare = info.get("payment", 0)
            total_walk = info.get("totalWalk", 0)
            
            segments = []
            
            for sub in best_path.get("subPath", []):
                traffic_type = sub.get("trafficType")
                station_id = None
                
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
                else:
                    continue
                    
                segments.append(RouteSegment(
                    segmentType=seg_type,
                    instruction=instruction,
                    durationMin=sub.get("sectionTime", 0),
                    startLocationName=sub.get("startName", "도보 출발"),
                    endLocationName=sub.get("endName", "도보 도착"),
                    stationId=station_id if station_id else None,
                    realTimeArrivalInfo=None, 
                    pathCoordinates=[] 
                ))
                
            return RouteResponse(
                totalTimeMin=total_time,
                totalFareWon=total_fare,
                totalWalkDistanceMeter=total_walk,
                segments=segments
            )
            
        except (KeyError, IndexError) as e:
            print(f"데이터 파싱 에러: {e}, 응답 원본: {data}")
            raise HTTPException(status_code=500, detail="API 응답 구조 파싱 실패")

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

    return RouteResponse(
        totalTimeMin=total_time,
        totalFareWon=total_fare,
        totalWalkDistanceMeter=total_walk,
        segments=merged_segments
    )