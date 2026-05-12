from pydantic import BaseModel
from typing import List, Optional
from enum import Enum

class OptType(str, Enum):
    MIN_TIME = "MIN_TIME"
    MIN_TRANSFER = "MIN_TRANSFER"
    MIN_WALK = "MIN_WALK"
    MIN_COST = "MIN_COST"

class LocationPoint(BaseModel):
    name: str
    latitude: float
    longitude: float

# 새로 추가된 위경도 좌표 데이터 모델
class Coordinate(BaseModel):
    latitude: float
    longitude: float

class RouteSegment(BaseModel):
    segmentType: str
    instruction: str
    durationMin: int
    startLocationName: str
    endLocationName: str
    
    # 지도를 띄우고 실시간 정보를 받기 위해 새롭게 추가된 부분
    pathCoordinates: Optional[List[Coordinate]] = None
    stationId: Optional[str] = None
    realTimeArrivalInfo: Optional[str] = None

class RouteRequest(BaseModel):
    startPoint: LocationPoint
    anchorPoints: List[LocationPoint]
    endPoint: LocationPoint
    optimizationType: OptType

class RouteResponse(BaseModel):
    totalTimeMin: int
    totalFareWon: int
    totalWalkDistanceMeter: int
    segments: List[RouteSegment]