from collector import frame_metrics, parse_battery, parse_foreground_app, parse_frame_times, parse_pss_total, parse_thermal

def test_foreground_package_variants():
    assert parse_foreground_app("mCurrentFocus=Window{abc u0 com.example.game/com.example.MainActivity}") == "com.example.game"
    assert parse_foreground_app("mFocusedApp=ActivityRecord{abc com.other.app/.Main}") == "com.other.app"

def test_framestats_and_metrics_are_defensive():
    output = """Flags,IntendedVsync,Vsync,OldestInputEvent,NewestInputEvent,HandleInputStart,AnimationStart,PerformTraversalsStart,DrawStart,SyncQueued,SyncStart,IssueDrawCommandsStart,SwapBuffers,FrameCompleted
0,1000000000,1000000000,0,0,0,0,0,0,0,0,0,1016000000,1020000000
0,1020000000,1020000000,0,0,0,0,0,0,0,0,0,1036000000,1040000000
malformed
"""
    times = parse_frame_times(output); assert times == [20.0, 20.0]
    metrics = frame_metrics(times); assert metrics["fps"] == 50.0; assert metrics["low_1_percent_fps"] == 50.0; assert metrics["frame_time_stddev_ms"] == 0.0

def test_other_parsers():
    assert parse_pss_total("Pss  Total:       12,345 kB") == 12345
    assert parse_battery("level: 87\ntemperature: 365") == {"battery_level_percent": 87.0, "battery_temperature_c": 36.5}
    assert parse_thermal("CPU temp: 72000") == [{"zone": "CPU", "temperature_c": 72.0}]
    assert frame_metrics([])["fps"] is None
