import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APPDATA = os.environ.get("APPDATA")
# According to Claude's README, templates go to:
# %APPDATA%\MoTeC\i2 Standard\Templates\<name>
WORKSPACE_DIR = os.path.join(APPDATA, "MoTeC", "i2 Standard", "Templates", "Flight Recorder")
WORKBOOKS_DIR = os.path.join(WORKSPACE_DIR, "Workbooks")
MATHS_DIR = os.path.join(WORKSPACE_DIR, "Maths")
ACTIVE_WORKSPACE_DIR = os.path.join(APPDATA, "MoTeC", "i2", "1.1", "i2 Standard", "Workspaces", "Flight Recorder")

os.makedirs(WORKBOOKS_DIR, exist_ok=True)
os.makedirs(MATHS_DIR, exist_ok=True)

workspace_xml = """<?xml version="1.0"?>
<Analysis DefaultLocale="C" Version="102" ShowOverlays="1" LayoutLocked="0" ExcludeUntrusted="1" IncludeOverlays="0" GraphTimeFormat="4" RemoveOnChanDblClick="0" ProfileId="Circuit" SetupSheetPasswdReqd="0" LapTimeDisplayFormat="0" LapNameInOut="0" AutoLapNumber="0" LapTrustPC="0" Splitter.PC="7500">
 <Resources/>
 <LatGTrackGen/>
 <DataSelect SelectFastestOnLoad="1" DefaultShowDisabled="0" MainColorSource="0">
  <DataSources/>
  <View Name="Laps" Color="128,240,0" View="Laps"/>
 </DataSelect>
 <ChannelsWindow/>
 <Workbooks Version="100" Name="My Workbooks" Active="Default">
  <Workbook XRef="Workbooks\\Default.i2wkb" Name="Default"/>
 </Workbooks>
</Analysis>"""

with open(os.path.join(WORKSPACE_DIR, "Workspace.i2wsp"), "w") as f:
    f.write(workspace_xml)

user_maths_xml = """<?xml version="1.0"?>
<Maths Locale="French_France.1252" DefaultLocale="C" Id="User">
 <MathItems>
  <MathExpression Id="Throttle Derivative" DisplayUnit="%/s" DisplayDPS="1" DisplayColorIndex="0" Interpolate="1" Script="derivative('tps_pct')" SampleRate="0" Unit="%/s"/>
 </MathItems>
</Maths>"""

with open(os.path.join(MATHS_DIR, "User.xml"), "w") as f:
    f.write(user_maths_xml)


def build_graph(name, traces, scale_min, scale_max, top_edge, bottom_edge):
    esc_name = name.replace("&", "&amp;")
    trace_xml = "\n".join([f'        <Trace Id="{t}" DisplayUnit="" DisplayDPS="0" Visible="1"/>' for t in traces])
    active_trace = traces[0] if traces else ""
    return f"""      <Graph Name="{esc_name}" ScaleMode="2" ScaleMin="{scale_min}" ScaleMax="{scale_max}">
       <Edges Mode="0" TopEdge="{top_edge}" BottomEdge="{bottom_edge}"/>
       <Edges Mode="1" TopEdge="0" BottomEdge="1000"/>
       <Lines>
        <Line Index="0" Enabled="1" Value="0" Color="210,210,210"/>
       </Lines>
       <Traces ActiveTrace="{active_trace}">
{trace_xml}
       </Traces>
      </Graph>"""

def build_worksheet(name, graphs):
    num_graphs = len(graphs)
    graph_xml = ""
    for i, g in enumerate(graphs):
        top = int(i * 1000 / num_graphs)
        bottom = int((i + 1) * 1000 / num_graphs)
        graph_xml += build_graph(g['name'], g['traces'], g['min'], g['max'], top, bottom) + "\n"
        
    return f"""  <Worksheet Version="200" Name="{name}" LinkTo="Laps" LockByGPSTime="0">
   <Controls Active="Time/Distance [1]">
    <MoTeC.TimeDistance Version="100" Name="Time/Distance [1]" PosPC="0,0,1000,1000" ShowCaption="0" AutoCaption="0" LinkZoom="1" ModeId="0" ShowDataLegend="1" ShowColorLegend="1" TraceLineWidth="1" TracePointSize="3" TraceStyle="0" LinkCursor="1" LinkDatum="1" ShowKeyValues="1" ShowKeyStats="1" KeyMode="0" KeyPos="1" ShowGridLines="1" ShowXAxis="1" ShowXAxisScrollBar="0" ShowYAxis="1" ShowYAxisScrollBar="1" ShowMarkers="1" ShowVSP="0" ShowBands="1" BandMode="1" OverlapMode="0" VariancePos="0.25" VarianceAutoScale="1">
     <Graphs ActivateChild="{graphs[0]['name'] if graphs else ''}">
{graph_xml}     </Graphs>
    </MoTeC.TimeDistance>
   </Controls>
  </Worksheet>"""

worksheets = [
    {
        "name": "Engine Basics",
        "graphs": [
            {"name": "Engine Speed", "traces": ["rpm"], "min": 0, "max": 10500},
            {"name": "Load & Throttle", "traces": ["tps_pct", "engine_load_pct"], "min": 0, "max": 100},
            {"name": "Ignition Timing", "traces": ["timing_advance_deg"], "min": -10, "max": 60},
            {"name": "Dwell & Injection", "traces": ["coil_dwell_ms", "injection_time_ms"], "min": 0, "max": 20},
        ]
    },
    {
        "name": "Diagnostics",
        "graphs": [
            {"name": "Engine Speed", "traces": ["rpm"], "min": 0, "max": 10500},
            {"name": "System Voltage", "traces": ["battery_volts"], "min": 10, "max": 15},
            {"name": "Coil Faults", "traces": ["coil_1_fault", "coil_2_fault", "coil_3_fault", "coil_4_fault"], "min": 0, "max": 2},
            {"name": "EFI & Sync", "traces": ["crank_sync", "efi_light", "tip_over"], "min": 0, "max": 2},
            {"name": "Temperatures", "traces": ["coolant_temp_c", "air_temp_c"], "min": 0, "max": 120},
        ]
    },
    {
        "name": "Derivatives",
        "graphs": [
            {"name": "Engine Speed", "traces": ["rpm"], "min": 0, "max": 10500},
            {"name": "Acceleration (drpm_dt)", "traces": ["drpm_dt"], "min": -10000, "max": 10000},
            {"name": "Throttle Derivative", "traces": ["Throttle Derivative", "dtps_dt"], "min": -100, "max": 100},
            {"name": "Voltage Delta (dvolts_dt)", "traces": ["dvolts_dt"], "min": -5, "max": 5},
        ]
    }
]

workbook_xml = f"""<?xml version="1.0"?>
<Workbook DefaultLocale="C" Version="200" Name="Default" Profile="Circuit" Active="Worksheets">
 <Worksheets Version="100" Name="Worksheets" PosPC="0,0,1000,1000" Active="Engine Basics">
"""
for ws in worksheets:
    workbook_xml += build_worksheet(ws['name'], ws['graphs']) + "\n"
workbook_xml += """ </Worksheets>
</Workbook>"""

with open(os.path.join(WORKBOOKS_DIR, "Default.i2wkb"), "w") as f:
    f.write(workbook_xml)

print(f"MoTeC Workspace template created at: {WORKSPACE_DIR}")

if os.path.exists(ACTIVE_WORKSPACE_DIR):
    active_maths = os.path.join(ACTIVE_WORKSPACE_DIR, "Maths")
    os.makedirs(active_maths, exist_ok=True)
    with open(os.path.join(active_maths, "User.xml"), "w") as f:
        f.write(user_maths_xml)
    print(f"Active MoTeC Workspace Maths updated at: {active_maths}")
