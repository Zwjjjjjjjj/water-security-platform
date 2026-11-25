#!/usr/bin/env python3
"""Coordinate conventions for Gemini track standardization.

Frames
------
SONAR_FAN:
    2-D imaging fan coordinates extracted from GLF.
    u_m = -range * sin(bearing), v_m = range * cos(bearing).
    u is the cross-fan axis; v is along the acoustic boresight.
    This is NOT automatically a horizontal XY frame.

PLATFORM_RFU:
    3-D platform-relative frame [right, forward, up].
    Conversion requires the physical fan roll/orientation sign.

ENU:
    [east, north, up]. Conversion additionally requires heading and
    platform reference-point position in ENU.

For the 2025-06-19 20:39:13 UTC Tanana River record, the collection log
states Vertical swath, 6 deg down tilt, 24 in sonar depth, Bow looking upstream.
It does not specify the sign of the 90-deg fan rotation, numerical absolute
heading, or an ENU translation. Those values must be supplied before a unique
ENU conversion is possible.
"""
from dataclasses import dataclass
import math
from typing import Optional, Tuple


@dataclass
class SonarExtrinsics:
    # +90 or -90 once physical vertical orientation is confirmed.
    fan_roll_deg: Optional[float] = None
    # Positive means boresight pitched downward.
    pitch_down_deg: float = 6.0
    # Heading: degrees clockwise from north. Required for ENU.
    heading_deg: Optional[float] = None
    # ENU position of the sonar/platform reference point. Required for ENU.
    east_m: Optional[float] = None
    north_m: Optional[float] = None
    up_m: Optional[float] = None


def polar_to_fan(range_m: float, bearing_deg: float) -> Tuple[float, float]:
    """Return (u_cross_fan, v_boresight)."""
    b = math.radians(bearing_deg)
    return -range_m * math.sin(b), range_m * math.cos(b)


def fan_to_platform_rfu(u_m: float, v_m: float, ext: SonarExtrinsics):
    """Convert fan coordinates to platform [right, forward, up].

    The fan is modeled as a horizontal native plane [right=u, forward=v, up=0],
    then rotated about the boresight/forward axis by fan_roll_deg to represent
    the physical swath orientation, then pitched downward by pitch_down_deg.
    """
    if ext.fan_roll_deg is None:
        raise ValueError("fan_roll_deg is unresolved; Vertical alone does not specify +90 vs -90 deg")

    phi = math.radians(ext.fan_roll_deg)
    # Rotate about forward axis: [right, forward, up].
    right = math.cos(phi) * u_m
    forward = v_m
    up = -math.sin(phi) * u_m

    # Pitch downward about the right axis. Positive pitch_down means forward tilts toward -up.
    a = math.radians(-ext.pitch_down_deg)
    forward2 = math.cos(a) * forward - math.sin(a) * up
    up2 = math.sin(a) * forward + math.cos(a) * up
    return right, forward2, up2


def platform_rfu_to_enu(right_m: float, forward_m: float, up_m_rel: float, ext: SonarExtrinsics):
    """Convert platform [right, forward, up] to ENU."""
    if ext.heading_deg is None:
        raise ValueError("heading_deg is required for ENU conversion")
    if ext.east_m is None or ext.north_m is None or ext.up_m is None:
        raise ValueError("east_m, north_m and up_m reference position are required for ENU conversion")

    psi = math.radians(ext.heading_deg)
    east_rel = right_m * math.cos(psi) + forward_m * math.sin(psi)
    north_rel = -right_m * math.sin(psi) + forward_m * math.cos(psi)
    return ext.east_m + east_rel, ext.north_m + north_rel, ext.up_m + up_m_rel


def fan_to_enu(u_m: float, v_m: float, ext: SonarExtrinsics):
    r, f, u = fan_to_platform_rfu(u_m, v_m, ext)
    return platform_rfu_to_enu(r, f, u, ext)
