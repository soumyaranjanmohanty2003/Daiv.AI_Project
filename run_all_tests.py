import os
import sys
import shutil
import subprocess
import webbrowser
from datetime import datetime

if shutil.which("maestro") is None:
    print("ERROR: Maestro not found in PATH")
    sys.exit(1)

flow_folder = os.path.join("flows", "home")

if not os.path.exists(flow_folder):
    print(f"Folder not found: {flow_folder}")
    sys.exit(1)

results = []

for file in os.listdir(flow_folder):
    if not file.endswith(".yaml"):
        continue

    file_path = os.path.join(flow_folder, file)

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            if not f.read().strip():
                results.append({
                    "name": file,
                    "status": "SKIPPED",
                    "duration": 0,
                    "stdout": "Empty YAML file",
                    "stderr": ""
                })
                continue
    except Exception as e:
        results.append({
            "name": file,
            "status": "FAIL",
            "duration": 0,
            "stdout": "",
            "stderr": str(e)
        })
        continue
    print(f"\nRunning: {file}")
    start = datetime.now()

    result = subprocess.run(
        ["maestro", "test", file_path],
        capture_output=True,
        text=True,
        shell=True
    )

    end = datetime.now()

    results.append({
        "name": file,
        "status": "PASS" if result.returncode == 0 else "FAIL",
        "duration": round((end - start).total_seconds(), 2),
        "stdout": result.stdout,
        "stderr": result.stderr
    })

total = len(results)
passed = len([x for x in results if x["status"] == "PASS"])
failed = len([x for x in results if x["status"] == "FAIL"])
skipped = len([x for x in results if x["status"] == "SKIPPED"])

executed = passed + failed
pass_rate = round((passed / executed) * 100, 2) if executed else 0

os.makedirs("reports", exist_ok=True)

counter = 1
while True:
    report_path = os.path.join(
        "reports",
        f"Regression_Report({counter}).html"
    )
    if not os.path.exists(report_path):
        break
    counter += 1

details_html = ""

for test in results:

    badge_class = {
        "PASS": "pass",
        "FAIL": "fail",
        "SKIPPED": "skip"
    }[test["status"]]

    details_html += f"""
    <details>
        <summary>
            <b>{test['name']}</b>
            <span class="badge {badge_class}">
                {test['status']}
            </span>
        </summary>

        <p><b>Duration:</b> {test['duration']} sec</p>

        <h4>Execution Logs</h4>

        <pre>
STDOUT:
{test['stdout']}

STDERR:
{test['stderr']}
        </pre>

    </details>
    <br>
    """

html = f"""
<!DOCTYPE html>
<html>

<head>
<meta charset="UTF-8">
<title>Maestro Regression Report</title>

<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/html2pdf.js/0.10.1/html2pdf.bundle.min.js"></script>

<style>

body {{
    font-family: Segoe UI, Arial, sans-serif;
    background:#f3f4f6;
    margin:0;
    padding:25px;
}}

.header {{
    background:linear-gradient(
        135deg,
        #4f46e5,
        #7c3aed
    );
    color:white;
    padding:25px;
    border-radius:18px;
    box-shadow:0 6px 18px rgba(0,0,0,.12);
}}

.header h1 {{
    margin:0;
}}

.cards {{
    display:flex;
    gap:15px;
    flex-wrap:wrap;
    margin-top:20px;
}}

.card {{
    flex:1;
    min-width:180px;
    background:white;
    padding:20px;
    border-radius:16px;
    text-align:center;
    box-shadow:0 4px 15px rgba(0,0,0,.08);
    transition:.3s;
}}

.card:hover {{
    transform:translateY(-4px);
}}

.card h2 {{
    margin:0;
}}

.section {{
    background:white;
    margin-top:25px;
    padding:25px;
    border-radius:16px;
    box-shadow:0 4px 15px rgba(0,0,0,.08);
}}

.chart-container {{
    width:350px;
    height:350px;
    margin:auto;
}}

.progress {{
    width:100%;
    height:14px;
    background:#e5e7eb;
    border-radius:20px;
    overflow:hidden;
    margin-top:20px;
}}

.progress-fill {{
    width:{pass_rate}%;
    height:100%;
    background:#22c55e;
}}

.badge {{
    padding:5px 12px;
    border-radius:20px;
    color:white;
    font-size:12px;
    margin-left:10px;
}}

.pass {{
    background:#22c55e;
}}

.fail {{
    background:#ef4444;
}}

.skip {{
    background:#f59e0b;
}}

summary {{
    cursor:pointer;
    padding:12px;
    font-size:15px;
    background:#f9fafb;
    border-radius:10px;
}}

details {{
    border:1px solid #e5e7eb;
    border-radius:10px;
    padding:10px;
}}

pre {{
    background:#111827;
    color:#e5e7eb;
    padding:15px;
    border-radius:12px;
    overflow:auto;
    max-height:300px;
    white-space:pre-wrap;
}}

button {{
    background:#4f46e5;
    color:white;
    border:none;
    border-radius:10px;
    padding:12px 24px;
    cursor:pointer;
    font-size:15px;
}}

button:hover {{
    opacity:.9;
}}

.download {{
    text-align:center;
    margin-top:20px;
}}

</style>
</head>

<body>

<div id="reportContent">

<div class="header">
    <h1>📱 Maestro Regression Report</h1>
    <p>Automation Execution Summary</p>
    <p>
        Generated On:
        {datetime.now().strftime("%d-%b-%Y %I:%M:%S %p")}
    </p>
</div>

<div class="cards">

    <div class="card">
        <h2>{total}</h2>
        <p>Total Tests</p>
    </div>

    <div class="card">
        <h2 style="color:#22c55e">{passed}</h2>
        <p>Passed</p>
    </div>

    <div class="card">
        <h2 style="color:#ef4444">{failed}</h2>
        <p>Failed</p>
    </div>

    <div class="card">
        <h2 style="color:#f59e0b">{skipped}</h2>
        <p>Skipped</p>
    </div>

    <div class="card">
        <h2>{pass_rate}%</h2>
        <p>Pass Rate</p>
    </div>

</div>

<div class="progress">
    <div class="progress-fill"></div>
</div>

<div class="section">

    <h2>📊 Execution Summary</h2>

    <div class="chart-container">
        <canvas id="resultChart"></canvas>
    </div>

</div>

<div class="section">

    <h2>🧪 Test Details</h2>

    {details_html}

</div>

</div>

<div class="download">
    <button onclick="downloadPDF()">
        Download PDF
    </button>
</div>

<script>

const ctx = document.getElementById('resultChart');

new Chart(ctx, {{

    type: 'doughnut',

    data: {{

        labels: ['Passed', 'Failed', 'Skipped'],

        datasets: [{{
            data: [{passed}, {failed}, {skipped}],

            backgroundColor: [
                '#22c55e',
                '#ef4444',
                '#f59e0b'
            ],

            hoverOffset: 25,

            spacing: 4,

            borderColor: '#ffffff',

            borderWidth: 4,

            borderRadius: 10

        }}]
    }},

    options: {{
        responsive: true,
        maintainAspectRatio: false,

        plugins: {{
            legend: {{
                position: 'bottom'
            }}
        }}
    }}
}});

function downloadPDF() {{

    document
        .querySelectorAll('details')
        .forEach(x => x.open = true);

    const element = document.getElementById('reportContent');

    const opt = {{

        margin: 10,

        filename: 'Regression_Report.pdf',

        image: {{
            type: 'jpeg',
            quality: 1
        }},

        html2canvas: {{
            scale: 2,
            useCORS: true,
            scrollY: 0
        }},

        jsPDF: {{
            unit: 'mm',
            format: 'a4',
            orientation: 'portrait'
        }},

        pagebreak: {{
            mode: ['avoid-all', 'css', 'legacy']
        }}
    }};

    html2pdf()
        .set(opt)
        .from(element)
        .save();
}}

</script>

</body>
</html>
"""

with open(report_path, "w", encoding="utf-8") as f:
    f.write(html)

webbrowser.open(os.path.abspath(report_path))

print("Report:", os.path.abspath(report_path))
