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

# [추가됨] 여러 버스를 묶고 실시간 도착 정보를 담는 스키마
class TransitOption(BaseModel):
    routeName: str
    arrivalTime1: Optional[str] = None
    arrivalTime2: Optional[str] = None

class RouteSegment(BaseModel):
    segmentType: str
    instruction: str
    durationMin: int
    startLocationName: str
    endLocationName: str
    pathCoordinates: Optional[List[Coordinate]] = None
    # [변경됨] 새로운 TransitOption 리스트
    transitOptions: Optional[List[TransitOption]] = []

class RouteRequest(BaseModel):
    startPoint: LocationPoint
    anchorPoints: List[LocationPoint]
    endPoint: LocationPoint
    optimizationType: OptType
    searchDate: Optional[str] = None

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