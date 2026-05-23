from fastapi import FastAPI
# [수정] RouteListResponse를 임포트 목록에 추가합니다.
from api.schemas import RouteRequest, RouteResponse, RouteListResponse
from api.services import process_optimized_route

app = FastAPI(title="Route Anchor API")

# [수정] response_model을 RouteListResponse로 변경합니다.
@app.post("/api/v1/route/optimize", response_model=RouteListResponse)
async def get_optimized_route(request: RouteRequest):
    print("---------------------------------")
    print(f"출발지: {request.startPoint.name}")
    print(f"경유지: {[p.name for p in request.anchorPoints]}")
    print(f"도착지: {request.endPoint.name}")
    print(f"탐색옵션: {request.optimizationType.name}")
    
    final_response = await process_optimized_route(request)
    
    return final_response