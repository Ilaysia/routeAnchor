from fastapi import FastAPI
from schemas import RouteRequest, RouteResponse
from services import process_optimized_route

app = FastAPI(title="Route Anchor API")

@app.post("/api/v1/route/optimize", response_model=RouteResponse)
async def get_optimized_route(request: RouteRequest):
    print("---------------------------------")
    print(f"출발지: {request.startPoint.name}")
    print(f"경유지: {[p.name for p in request.anchorPoints]}")
    print(f"도착지: {request.endPoint.name}")
    print(f"탐색옵션: {request.optimizationType.name}")
    
    final_response = await process_optimized_route(request)
    
    return final_response