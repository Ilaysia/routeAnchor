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

class Coordinate(BaseModel):
    latitude: float
    longitude: float

class RouteSegment(BaseModel):
    segmentType: str
    instruction: str
    durationMin: int
    startLocationName: str
    endLocationName: str
    pathCoordinates: Optional[List[Coordinate]] = None
    stationId: Optional[str] = None
    realTimeArrivalInfo: Optional[str] = None

class RouteRequest(BaseModel):
    startPoint: LocationPoint
    anchorPoints: List[LocationPoint]
    endPoint: LocationPoint
    optimizationType: OptType
    searchDate: Optional[str] = None  # 🌟 [추가] yyyyMMddHHmm 형식의 시간 데이터 수신용

class RouteResponse(BaseModel):
    totalTimeMin: int
    totalFareWon: int
    totalWalkDistanceMeter: int
    segments: List[RouteSegment]
    startCoordinate: Optional[Coordinate] = None
    endCoordinate: Optional[Coordinate] = None
    anchorCoordinates: List[Coordinate] = []

class RouteListResponse(BaseModel):
    routes: List[RouteResponse]