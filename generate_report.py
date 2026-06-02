#!/usr/bin/env python3
"""
Memory Benchmark Report Generator

Generates PDF reports with visualizations.

Usage:
    python3 generate_report.py --all                # Run all tests and generate PDF report
    python3 generate_report.py --test <test_name> # Run single test and generate report
    python3 generate_report.py --list              # List available tests
"""

import os
import sys
import subprocess
import argparse
import getpass
from datetime import datetime

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch, cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, Image
    from reportlab.graphics.shapes import Drawing, Rect
    from reportlab.graphics import renderPDF
    HAS_REPORTLAB = True
except ImportError:
    HAS_REPORTLAB = False

try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BUILD_DIR = os.path.join(SCRIPT_DIR, "bin")
REPORTS_DIR = os.path.join(SCRIPT_DIR, "reports")
CHARTS_DIR = os.path.join(REPORTS_DIR, "charts")
os.makedirs(CHARTS_DIR, exist_ok=True)

# Global sudo password cache
_sudo_password = None

TESTS = {
    "cache_hierarchy": {
        "name": "Cache Hierarchy Test",
        "description": "L1/L2/L3 cache and RAM latency/bandwidth",
        "bin": "test_cache_hierarchy",
        "report": "cache_hierarchy_report.md",
        "section": "Cache & Memory Hierarchy"
    },
    "memory_bandwidth": {
        "name": "Memory Bandwidth Test",
        "description": "Multi-channel memory read/write/copy bandwidth",
        "bin": "test_memory_bandwidth",
        "report": "memory_bandwidth_report.md",
        "section": "Memory Bandwidth"
    },
    "inter_core": {
        "name": "Inter-Core Latency Test",
        "description": "Full NxN inter-core latency matrix + CAS throughput",
        "bin": "test_inter_core",
        "report": "inter_core_latency_report.md",
        "section": "Inter-Core Communication"
    },
    "cpu_alu": {
        "name": "CPU ALU Test",
        "description": "Single-core integer operations (add/mul/div/mod)",
        "bin": "test_cpu_alu",
        "report": "cpu_alu_report.md",
        "section": "CPU Integer (ALU)"
    },
    "cpu_float": {
        "name": "CPU Float Test",
        "description": "Single-core floating-point operations (float/double/sqrt)",
        "bin": "test_cpu_float",
        "report": "cpu_float_report.md",
        "section": "CPU Floating-Point"
    },
    "cpu_branch": {
        "name": "CPU Branch Test",
        "description": "Branch prediction performance (various patterns)",
        "bin": "test_cpu_branch",
        "report": "cpu_branch_report.md",
        "section": "CPU Branch Prediction"
    },
    "cpu_multi": {
        "name": "CPU Multi-Core Test",
        "description": "Multi-core speedup and efficiency",
        "bin": "test_cpu_multi",
        "report": "cpu_multi_core_report.md",
        "section": "CPU Multi-Core Scaling"
    }
}


def get_sudo_password():
    """Get sudo password once and cache it"""
    global _sudo_password
    if _sudo_password is None:
        try:
            _sudo_password = getpass.getpass("sudo password: ")
        except EOFError:
            _sudo_password = ""
    return _sudo_password


def verify_sudo_once():
    """Verify sudo once at the beginning, cache the credential"""
    password = get_sudo_password()
    if not password:
        return False
    try:
        proc = subprocess.run(
            ['sudo', '-S', '-v'],
            input=password + '\n',
            text=True,
            capture_output=True
        )
        return proc.returncode == 0
    except Exception:
        return False


def ensure_dirs():
    """Ensure build and reports directories exist"""
    os.makedirs(BUILD_DIR, exist_ok=True)
    os.makedirs(REPORTS_DIR, exist_ok=True)
    os.makedirs(CHARTS_DIR, exist_ok=True)


def build_test(test_name):
    """Build a single test"""
    if test_name not in TESTS:
        print(f"Error: Unknown test '{test_name}'")
        return False

    test = TESTS[test_name]
    bin_path = os.path.join(BUILD_DIR, test["bin"])

    if os.path.exists(bin_path):
        return True

    print(f"Building {test_name}...")
    result = subprocess.run(
        ["make", "-C", SCRIPT_DIR, test["bin"]],
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        print(f"Build failed:\n{result.stderr}")
        return False
    return True


def run_test(test_name, verbose=False):
    """Run a single test and generate its report"""
    global _sudo_password
    if test_name not in TESTS:
        print(f"Error: Unknown test '{test_name}'")
        return False

    test = TESTS[test_name]
    bin_path = os.path.join(BUILD_DIR, test["bin"])

    if not os.path.exists(bin_path):
        if not build_test(test_name):
            return False

    print(f"\n{'='*60}")
    print(f"Running: {test['name']}")
    print(f"{'='*60}")

    if _sudo_password:
        result = subprocess.run(
            ['sudo', '-S', bin_path],
            cwd=SCRIPT_DIR,
            input=_sudo_password + '\n',
            text=True,
            capture_output=False
        )
    else:
        result = subprocess.run(
            [bin_path],
            cwd=SCRIPT_DIR,
            capture_output=False
        )

    if result.returncode != 0:
        print(f"Test failed: {test_name}")
        return False

    report_path = os.path.join(REPORTS_DIR, test["report"])
    if os.path.exists(report_path):
        print(f"\n[Report] Generated: {report_path}")
        return True
    else:
        print(f"\n[Warning] Report file not found: {report_path}")
        return True


def list_tests():
    """List all available tests"""
    print("\nAvailable tests:\n")
    print(f"{'Test Name':<20} | {'Description':<45} | {'Binary'}")
    print("-" * 90)

    for name, info in TESTS.items():
        bin_path = os.path.join(BUILD_DIR, info["bin"])
        status = "[x]" if os.path.exists(bin_path) else "[ ]"
        print(f"{status} {name:<18} | {info['description']:<45} | {info['bin']}")

    print("\nUsage:")
    print("  python3 generate_report.py --all           # Run all tests")
    print("  python3 generate_report.py --test <name>   # Run single test")


def parse_cache_report(report_path):
    """Parse cache hierarchy report data"""
    data = {'sizes': [], 'rd_lat': [], 'wr_lat': [], 'bw': [], 'expected': []}
    try:
        with open(report_path, 'r') as f:
            content = f.read()
            in_table = False
            for line in content.split('\n'):
                if 'RdLat(ns)' in line or 'Rd Lat' in line:
                    in_table = True
                    continue
                if in_table and '|' in line:
                    parts = [p.strip() for p in line.split('|')]
                    # parts[0]=empty, parts[1]=Size, parts[2]=RdLat, parts[3]=WrLat, parts[4]=BW, parts[5]=Expected
                    if len(parts) >= 6:
                        try:
                            size = parts[1]
                            rd_lat = float(parts[2])
                            wr_lat = float(parts[3])
                            bw = float(parts[4])
                            expected = parts[5]
                            # Filter out separator lines
                            if size.replace('.', '').replace('KB', '').replace('MB', '').replace('GB', '').isdigit() or size[-2:] in ['KB', 'MB', 'GB']:
                                data['sizes'].append(size)
                                data['rd_lat'].append(rd_lat)
                                data['wr_lat'].append(wr_lat)
                                data['bw'].append(bw)
                                data['expected'].append(expected)
                        except (ValueError, IndexError):
                            pass
                elif in_table and line.strip() == '':
                    break
    except Exception as e:
        print(f"Error parsing cache report: {e}")
    return data


def parse_multi_core_report(report_path):
    """Parse multi-core report data for scaling charts"""
    data = {'operations': {}, 'thread_counts': [1, 2, 4, 8, 16, 24]}
    try:
        with open(report_path, 'r') as f:
            content = f.read()
            in_table = False

            for line in content.split('\n'):
                if 'Operation | Category' in line or '|-----------|' in line:
                    in_table = True
                    continue
                if in_table and '|' in line:
                    parts = [p.strip() for p in line.split('|')]
                    if len(parts) >= 7:
                        try:
                            op = parts[1]
                            speedup_str = parts[5].replace('x', '').strip()
                            eff_str = parts[6].replace('%', '').strip()
                            speedup = float(speedup_str)
                            eff = float(eff_str)
                            if op not in data['operations']:
                                data['operations'][op] = {'speedup': [], 'efficiency': []}
                            data['operations'][op]['speedup'].append(speedup)
                            data['operations'][op]['efficiency'].append(eff)
                        except:
                            pass
                if in_table and line.strip() == '' and len(data['operations']) > 0:
                    break
    except Exception as e:
        print(f"Error parsing multi-core report: {e}")
    return data


def create_cache_latency_chart(data, output_path):
    """Create cache latency visualization chart"""
    if not HAS_MATPLOTLIB or not data['sizes']:
        return None

    try:
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))

        # Latency chart
        x = range(len(data['sizes']))
        width = 0.35
        ax1.bar([i - width/2 for i in x], data['rd_lat'], width, label='Read', color='steelblue')
        ax1.bar([i + width/2 for i in x], data['wr_lat'], width, label='Write', color='coral')
        ax1.set_xlabel('Size')
        ax1.set_ylabel('Latency (ns)')
        ax1.set_title('Cache Hierarchy Latency')
        ax1.set_xticks(x)
        ax1.set_xticklabels(data['sizes'], rotation=45, ha='right')
        ax1.legend()
        ax1.grid(True, alpha=0.3)

        # Bandwidth chart
        ax2.plot(data['sizes'], data['bw'], marker='o', linewidth=2, color='green')
        ax2.set_xlabel('Size')
        ax2.set_ylabel('Bandwidth (MB/s)')
        ax2.set_title('Cache Hierarchy Bandwidth')
        ax2.set_xticks(range(len(data['sizes'])))
        ax2.set_xticklabels(data['sizes'], rotation=45, ha='right')
        ax2.grid(True, alpha=0.3)
        ax2.set_yscale('log')

        plt.tight_layout()
        plt.savefig(output_path, dpi=150, bbox_inches='tight')
        plt.close()
        return output_path
    except Exception as e:
        print(f"Error creating cache chart: {e}")
        return None


def create_multi_core_scaling_chart(data, output_path):
    """Create multi-core scaling visualization"""
    if not HAS_MATPLOTLIB or not data['operations']:
        return None

    try:
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))
        threads = data['thread_counts']

        colors = ['steelblue', 'coral', 'green', 'orange']
        for i, (op, values) in enumerate(data['operations'].items()):
            if values['speedup']:
                ax1.plot(threads[:len(values['speedup'])], values['speedup'],
                        marker='o', linewidth=2, label=op, color=colors[i % len(colors)])
                ax2.plot(threads[:len(values['efficiency'])], values['efficiency'],
                        marker='s', linewidth=2, label=op, color=colors[i % len(colors)])

        # Ideal scaling line
        ax1.plot(threads, threads, 'k--', alpha=0.5, label='Ideal')

        ax1.set_xlabel('Threads')
        ax1.set_ylabel('Speedup')
        ax1.set_title('Multi-Core Speedup')
        ax1.legend(loc='upper left')
        ax1.grid(True, alpha=0.3)
        ax1.set_xticks(threads)

        ax2.axhline(y=100, color='k', linestyle='--', alpha=0.5, label='Ideal (100%)')
        ax2.set_xlabel('Threads')
        ax2.set_ylabel('Efficiency (%)')
        ax2.set_title('Multi-Core Efficiency')
        ax2.legend(loc='upper right')
        ax2.grid(True, alpha=0.3)
        ax2.set_xticks(threads)
        ax2.set_ylim(0, 110)

        plt.tight_layout()
        plt.savefig(output_path, dpi=150, bbox_inches='tight')
        plt.close()
        return output_path
    except Exception as e:
        print(f"Error creating multi-core chart: {e}")
        return None


def create_bandwidth_chart(report_path, output_path):
    """Create memory bandwidth chart"""
    if not HAS_MATPLOTLIB:
        return None

    try:
        with open(report_path, 'r') as f:
            content = f.read()
            read_bw, write_bw, copy_bw = None, None, None
            in_table = False
            seen_data = False

            for line in content.split('\n'):
                if 'Bandwidth Results' in line:
                    in_table = True
                    continue
                if in_table and '|' in line:
                    parts = [p.strip() for p in line.split('|')]
                    if len(parts) >= 3:
                        op = parts[1]
                        bw_str = parts[2].replace(',', '')
                        try:
                            bw = float(bw_str)
                            if op == 'Read':
                                read_bw = bw
                                seen_data = True
                            elif op == 'Write':
                                write_bw = bw
                                seen_data = True
                            elif op == 'Copy':
                                copy_bw = bw
                                seen_data = True
                        except ValueError:
                            pass
                if in_table and seen_data and line.strip() == '':
                    break

        if read_bw is None and write_bw is None and copy_bw is None:
            return None

        fig, ax = plt.subplots(figsize=(8, 5))
        categories = ['Read', 'Write', 'Copy']
        values = [read_bw, write_bw, copy_bw]
        colors = ['steelblue', 'coral', 'green']

        bars = ax.bar(categories, values, color=colors)
        ax.set_ylabel('Bandwidth (MB/s)')
        ax.set_title('Memory Bandwidth')
        ax.grid(True, alpha=0.3, axis='y')

        for bar, val in zip(bars, values):
            ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 500,
                   f'{val:.0f}', ha='center', va='bottom')

        plt.tight_layout()
        plt.savefig(output_path, dpi=150, bbox_inches='tight')
        plt.close()
        return output_path
    except Exception as e:
        print(f"Error creating bandwidth chart: {e}")
        return None


def create_cpu_chart(report_path, output_path, chart_type='alu'):
    """Create CPU performance chart"""
    if not HAS_MATPLOTLIB:
        return None

    try:
        with open(report_path, 'r') as f:
            content = f.read()

        ops, ns_ops = [], []
        in_table = False

        for line in content.split('\n'):
            if 'ns/op' in line and '|' in line:
                in_table = True
                continue
            if in_table and '|' in line:
                parts = [p.strip() for p in line.split('|')]
                if len(parts) >= 5 and parts[1] not in ['Operation', '---', '-', '']:
                    try:
                        op = parts[1]
                        ns_op = float(parts[4])
                        ops.append(op)
                        ns_ops.append(ns_op)
                    except:
                        pass
            elif in_table and line.strip() == '':
                break

        if not ops:
            return None

        fig, ax = plt.subplots(figsize=(10, 6))
        y_pos = range(len(ops))
        ax.barh(y_pos, ns_ops, color='steelblue')
        ax.set_yticks(y_pos)
        ax.set_yticklabels(ops)
        ax.set_xlabel('ns/op')
        ax.set_title(f'CPU {chart_type.upper()} Operations - Latency')
        ax.grid(True, alpha=0.3, axis='x')
        ax.invert_yaxis()

        plt.tight_layout()
        plt.savefig(output_path, dpi=150, bbox_inches='tight')
        plt.close()
        return output_path
    except Exception as e:
        print(f"Error creating CPU chart: {e}")
        return None


def create_inter_core_heatmap(report_path, output_path):
    """Create inter-core latency heatmap"""
    if not HAS_MATPLOTLIB:
        return None

    try:
        matrix = []
        in_latency_matrix = False

        with open(report_path, 'r') as f:
            for line in f:
                if 'CAS Throughput' in line:
                    break
                if 'CAS Latency' in line and 'ns' in line:
                    in_latency_matrix = True
                    continue
                if in_latency_matrix and '|' in line:
                    parts = [p.strip() for p in line.split('|')]
                    # Skip header and separator lines
                    if len(parts) > 1 and '**Core**' in parts[1]:
                        continue
                    if all('---' in p or p == '' for p in parts):
                        continue
                    # Skip first (row label) and last (trailing empty from split)
                    vals = [p for p in parts[1:] if p]
                    row_vals = []
                    for p in vals[1:]:  # Skip row number at vals[0]
                        try:
                            row_vals.append(float(p))
                        except:
                            if p == '-':
                                row_vals.append(0)
                    if len(row_vals) >= 4:
                        matrix.append(row_vals)

        if not matrix or len(matrix) < 4:
            return None

        fig, ax = plt.subplots(figsize=(12, 10))
        im = ax.imshow(matrix, cmap='YlOrRd', aspect='auto')
        ax.set_title('Inter-Core CAS Latency (ns)', fontsize=14)
        ax.set_xlabel('Core ID', fontsize=12)
        ax.set_ylabel('Core ID', fontsize=12)

        n = len(matrix)
        ax.set_xticks(range(n))
        ax.set_yticks(range(n))
        ax.set_xticklabels(range(n))
        ax.set_yticklabels(range(n))

        # Add text annotations in each cell
        for i in range(n):
            for j in range(len(matrix[i])):
                val = matrix[i][j]
                row_min = min(matrix[i])
                row_max = max(matrix[i])
                text_color = 'white' if val > (row_max + row_min) / 2 else 'black'
                ax.text(j, i, f'{val:.1f}', ha='center', va='center',
                       color=text_color, fontsize=6)

        cbar = fig.colorbar(im, ax=ax)
        cbar.set_label('Latency (ns)', fontsize=10)

        plt.tight_layout()
        plt.savefig(output_path, dpi=150, bbox_inches='tight')
        plt.close()
        return output_path
    except Exception as e:
        print(f"Error creating heatmap: {e}")
        return None


def extract_bandwidth_data(report_path):
    """Extract bandwidth data from report"""
    data = []
    in_table = False
    try:
        with open(report_path, 'r') as f:
            for line in f:
                if 'Bandwidth Results' in line:
                    in_table = True
                    continue
                if in_table and '|' in line:
                    parts = [p.strip() for p in line.split('|')]
                    if len(parts) >= 3 and parts[1] in ['Read', 'Write', 'Copy']:
                        try:
                            bw = float(parts[2].replace(',', ''))
                            eff = parts[3].strip() if len(parts) > 3 else '-'
                            data.append((parts[1], bw, eff))
                        except:
                            pass
                if in_table and line.strip() == '' and len(data) > 0:
                    break
    except:
        pass
    return data

def extract_cpu_alu_data(report_path):
    """Extract CPU ALU data from report"""
    data = []
    in_table = False
    try:
        with open(report_path, 'r') as f:
            for line in f:
                if 'ns/op' in line and '|' in line:
                    in_table = True
                    continue
                if in_table and '|' in line:
                    parts = [p.strip() for p in line.split('|')]
                    # Filter out header and separator lines
                    if len(parts) >= 6 and parts[1] not in ['Operation', '---', '-', ''] and not parts[2].startswith('-'):
                        data.append([parts[1], parts[2], parts[3], parts[4], parts[5]])
                if in_table and line.strip() == '' and len(data) > 0:
                    break
    except:
        pass
    return data

def extract_cpu_float_data(report_path):
    """Extract CPU float data from report"""
    data = []
    in_table = False
    try:
        with open(report_path, 'r') as f:
            for line in f:
                if 'ns/op' in line and '|' in line:
                    in_table = True
                    continue
                if in_table and '|' in line:
                    parts = [p.strip() for p in line.split('|')]
                    if len(parts) >= 6 and parts[1] not in ['Operation', '---', '-', ''] and not parts[2].startswith('-'):
                        data.append([parts[1], parts[2], parts[3], parts[4], parts[5], parts[6] if len(parts) > 6 else ''])
                if in_table and line.strip() == '' and len(data) > 0:
                    break
    except:
        pass
    return data

def extract_multi_core_data(report_path):
    """Extract multi-core scaling data from report"""
    data = []
    in_table = False
    try:
        with open(report_path, 'r') as f:
            for line in f:
                if 'Operation | Category' in line or '|-----------|' in line:
                    in_table = True
                    continue
                if in_table and '|' in line:
                    parts = [p.strip() for p in line.split('|')]
                    if len(parts) >= 7 and parts[1] not in ['Operation', '---', '-', ''] and not parts[2].startswith('-'):
                        data.append([parts[1], parts[2], parts[3], parts[4], parts[5], parts[6]])
                if in_table and line.strip() == '' and len(data) > 0:
                    break
    except:
        pass
    return data

def extract_inter_core_stats(report_path):
    """Extract inter-core latency statistics"""
    stats = {}
    try:
        with open(report_path, 'r') as f:
            content = f.read()
            for line in content.split('\n'):
                if line.startswith('Min:'):
                    parts = line.replace('Min:', '').replace('Max:', '').replace('Avg:', '').split(',')
                    for p in parts:
                        if 'Min' in line:
                            stats['Min Latency'] = p.replace('Min', '').strip()
                        elif 'Max' in line:
                            stats['Max Latency'] = p.replace('Max', '').strip()
                        elif 'Avg' in line:
                            stats['Avg Latency'] = p.replace('Avg', '').strip()
    except:
        pass
    return stats

def generate_pdf_report():
    """Generate final PDF report with charts"""
    if not HAS_REPORTLAB:
        print("\n[Info] reportlab not installed. Skipping PDF generation.")
        print("Install with: pip install reportlab matplotlib numpy")
        return True  # Return True so the test doesn't fail

    print("\n" + "="*70)
    print("GENERATING PDF REPORT")
    print("="*70)

    # Get device info
    import socket
    hostname = socket.gethostname()
    try:
        with open('/etc/machine-id', 'r') as f:
            device_id = f.read().strip()[:8]
    except:
        device_id = hostname[:8]
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    pdf_path = os.path.join(REPORTS_DIR, "benchmark_report.pdf")
    doc = SimpleDocTemplate(pdf_path, pagesize=A4,
                           rightMargin=72, leftMargin=72,
                           topMargin=72, bottomMargin=72)

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=24,
        spaceAfter=30
    )
    heading_style = ParagraphStyle(
        'CustomHeading',
        parent=styles['Heading2'],
        fontSize=16,
        spaceAfter=12,
        spaceBefore=20
    )

    # Watermark function
    def add_watermark(canvas, doc):
        canvas.saveState()
        canvas.setFont('Helvetica', 8)
        canvas.setFillColor(colors.grey)
        # Bottom right watermark
        wm_text = f"{timestamp} | {hostname} | {device_id}"
        canvas.drawRightString(A4[0] - 72, 36, wm_text)
        # Diagonal watermark on each page
        canvas.setFillColor(colors.Color(0.9, 0.9, 0.9, alpha=0.3))
        canvas.rotate(45)
        canvas.drawString(100, 0, "CONFIDENTIAL")
        canvas.drawRightString(500, 0, "CONFIDENTIAL")
        canvas.restoreState()

    story = []

    # Title
    story.append(Paragraph("Memory Benchmark Report", title_style))
    story.append(Paragraph(f"<i>Generated: {timestamp}</i>", styles['Normal']))
    story.append(Spacer(1, 10))

    # Device Info Box
    device_info = [
        ['Device ID', device_id],
        ['Hostname', hostname],
        ['Report Time', timestamp],
    ]
    t = Table(device_info, colWidths=[1.5*inch, 3*inch])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, -1), colors.Color(0.95, 0.95, 1.0)),
        ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOX', (0, 0), (-1, -1), 1, colors.Color(0.7, 0.7, 0.9)),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.Color(0.8, 0.8, 0.9)),
    ]))
    story.append(t)
    story.append(Spacer(1, 20))

    # System Info Section (at beginning)
    cache_report = os.path.join(REPORTS_DIR, "cache_hierarchy_report.md")
    sys_info = []
    if os.path.exists(cache_report):
        with open(cache_report, 'r') as f:
            content = f.read()
            in_config = False
            for line in content.split('\n'):
                if 'System Configuration' in line:
                    in_config = True
                    continue
                if in_config and '|' in line and '-' not in line:
                    parts = [p.strip() for p in line.split('|')]
                    if len(parts) >= 3 and parts[1]:
                        sys_info.append([parts[1], parts[2]])
                elif in_config and line.strip() == '':
                    break

    if sys_info:
        story.append(Paragraph("System Configuration", heading_style))
        t = Table(sys_info, colWidths=[2*inch, 3*inch])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (0, -1), colors.lightgrey),
            ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
            ('TOPPADDING', (0, 0), (-1, -1), 8),
            ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ]))
        story.append(t)
        story.append(Spacer(1, 20))

    # Generate charts
    charts_generated = []

    # Cache hierarchy chart
    if os.path.exists(cache_report):
        data = parse_cache_report(cache_report)
        chart_path = os.path.join(CHARTS_DIR, "cache_latency.png")
        if create_cache_latency_chart(data, chart_path):
            charts_generated.append(('Cache Hierarchy', chart_path))

    # Multi-core scaling chart
    multi_report = os.path.join(REPORTS_DIR, "cpu_multi_core_report.md")
    if os.path.exists(multi_report):
        data = parse_multi_core_report(multi_report)
        chart_path = os.path.join(CHARTS_DIR, "multi_core_scaling.png")
        if create_multi_core_scaling_chart(data, chart_path):
            charts_generated.append(('Multi-Core Scaling', chart_path))

    # Memory bandwidth chart
    bw_report = os.path.join(REPORTS_DIR, "memory_bandwidth_report.md")
    if os.path.exists(bw_report):
        chart_path = os.path.join(CHARTS_DIR, "memory_bandwidth.png")
        if create_bandwidth_chart(bw_report, chart_path):
            charts_generated.append(('Memory Bandwidth', chart_path))

    # CPU ALU chart
    alu_report = os.path.join(REPORTS_DIR, "cpu_alu_report.md")
    if os.path.exists(alu_report):
        chart_path = os.path.join(CHARTS_DIR, "cpu_alu.png")
        if create_cpu_chart(alu_report, chart_path, 'alu'):
            charts_generated.append(('CPU ALU', chart_path))

    # CPU Float chart
    float_report = os.path.join(REPORTS_DIR, "cpu_float_report.md")
    if os.path.exists(float_report):
        chart_path = os.path.join(CHARTS_DIR, "cpu_float.png")
        if create_cpu_chart(float_report, chart_path, 'float'):
            charts_generated.append(('CPU Float', chart_path))

    # Inter-core heatmap
    ic_report = os.path.join(REPORTS_DIR, "inter_core_latency_report.md")
    if os.path.exists(ic_report):
        chart_path = os.path.join(CHARTS_DIR, "inter_core_heatmap.png")
        if create_inter_core_heatmap(ic_report, chart_path):
            charts_generated.append(('Inter-Core Latency', chart_path))

    # Cache Hierarchy Section
    if os.path.exists(cache_report):
        story.append(Paragraph("Cache Hierarchy Test", heading_style))
        data = parse_cache_report(cache_report)
        if data['sizes']:
            table_data = [['Size', 'Rd Lat (ns)', 'Wr Lat (ns)', 'BW (MB/s)', 'Expected']]
            for i, size in enumerate(data['sizes']):
                if i < len(data['rd_lat']):
                    table_data.append([
                        size,
                        f"{data['rd_lat'][i]:.2f}",
                        f"{data['wr_lat'][i]:.2f}",
                        f"{data['bw'][i]:.0f}",
                        data['expected'][i] if i < len(data['expected']) else ''
                    ])
            t = Table(table_data, colWidths=[1*inch, 1.2*inch, 1.2*inch, 1.2*inch, 1*inch])
            t.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
                ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
                ('FONTSIZE', (0, 0), (-1, -1), 8),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
            ]))
            story.append(t)
            story.append(Spacer(1, 10))
        chart_path = os.path.join(CHARTS_DIR, "cache_latency.png")
        if os.path.exists(chart_path):
            story.append(Image(chart_path, width=6*inch, height=4*inch))
        story.append(PageBreak())

    # Memory Bandwidth Section
    bw_report = os.path.join(REPORTS_DIR, "memory_bandwidth_report.md")
    if os.path.exists(bw_report):
        story.append(Paragraph("Memory Bandwidth Test", heading_style))
        bw_data = extract_bandwidth_data(bw_report)
        if bw_data:
            table_data = [['Operation', 'Bandwidth (MB/s)', 'Efficiency']]
            for op, bw, eff in bw_data:
                table_data.append([op, f"{bw:.2f}", eff])
            t = Table(table_data, colWidths=[1.5*inch, 1.5*inch, 1.5*inch])
            t.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
                ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
                ('FONTSIZE', (0, 0), (-1, -1), 10),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                ('TOPPADDING', (0, 0), (-1, -1), 6),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
            ]))
            story.append(t)
            story.append(Spacer(1, 10))
        chart_path = os.path.join(CHARTS_DIR, "memory_bandwidth.png")
        if os.path.exists(chart_path):
            story.append(Image(chart_path, width=6*inch, height=4*inch))
        story.append(PageBreak())

    # CPU ALU Section
    alu_report = os.path.join(REPORTS_DIR, "cpu_alu_report.md")
    if os.path.exists(alu_report):
        story.append(Paragraph("CPU ALU Test", heading_style))
        alu_data = extract_cpu_alu_data(alu_report)
        if alu_data:
            table_data = [['Operation', 'Ops/sec', 'ns/op', 'CPI', 'IPC']]
            for row in alu_data:
                if len(row) >= 5:
                    table_data.append([row[0], f"{float(row[1]):.0f}", row[2], row[3], row[4]])
            t = Table(table_data, colWidths=[1.2*inch, 1.5*inch, 1*inch, 0.8*inch, 0.8*inch])
            t.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
                ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
                ('FONTSIZE', (0, 0), (-1, -1), 8),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
            ]))
            story.append(t)
            story.append(Spacer(1, 10))
        chart_path = os.path.join(CHARTS_DIR, "cpu_alu.png")
        if os.path.exists(chart_path):
            story.append(Image(chart_path, width=6*inch, height=4*inch))
        story.append(PageBreak())

    # CPU Float Section
    float_report = os.path.join(REPORTS_DIR, "cpu_float_report.md")
    if os.path.exists(float_report):
        story.append(Paragraph("CPU Floating-Point Test", heading_style))
        float_data = extract_cpu_float_data(float_report)
        if float_data:
            table_data = [['Operation', 'Type', 'Ops/sec', 'ns/op', 'CPI', 'IPC']]
            for row in float_data:
                if len(row) >= 6:
                    table_data.append([row[0], row[1], f"{float(row[2]):.0f}", row[3], row[4], row[5]])
            t = Table(table_data, colWidths=[1*inch, 0.8*inch, 1.3*inch, 0.8*inch, 0.7*inch, 0.7*inch])
            t.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
                ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
                ('FONTSIZE', (0, 0), (-1, -1), 7),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
                ('TOPPADDING', (0, 0), (-1, -1), 3),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
            ]))
            story.append(t)
            story.append(Spacer(1, 10))
        chart_path = os.path.join(CHARTS_DIR, "cpu_float.png")
        if os.path.exists(chart_path):
            story.append(Image(chart_path, width=6*inch, height=4*inch))
        story.append(PageBreak())

    # Multi-Core Section
    multi_report = os.path.join(REPORTS_DIR, "cpu_multi_core_report.md")
    if os.path.exists(multi_report):
        story.append(Paragraph("Multi-Core Scaling Test", heading_style))
        multi_data = extract_multi_core_data(multi_report)
        if multi_data:
            table_data = [['Operation', 'Threads', 'Time(ms)', 'Speedup', 'Efficiency', 'Status']]
            for row in multi_data:
                if len(row) >= 6:
                    table_data.append(row)
            t = Table(table_data, colWidths=[0.9*inch, 0.7*inch, 0.9*inch, 0.8*inch, 0.9*inch, 1.2*inch])
            t.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
                ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
                ('FONTSIZE', (0, 0), (-1, -1), 7),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
                ('TOPPADDING', (0, 0), (-1, -1), 2),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
            ]))
            story.append(t)
            story.append(Spacer(1, 10))
        chart_path = os.path.join(CHARTS_DIR, "multi_core_scaling.png")
        if os.path.exists(chart_path):
            story.append(Image(chart_path, width=6*inch, height=4*inch))
        story.append(PageBreak())

    # Inter-Core Section
    ic_report = os.path.join(REPORTS_DIR, "inter_core_latency_report.md")
    if os.path.exists(ic_report):
        story.append(Paragraph("Inter-Core Latency Test", heading_style))
        ic_stats = extract_inter_core_stats(ic_report)
        if ic_stats:
            table_data = [['Metric', 'Value']]
            for k, v in ic_stats.items():
                table_data.append([k, v])
            t = Table(table_data, colWidths=[2*inch, 2*inch])
            t.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
                ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
                ('FONTSIZE', (0, 0), (-1, -1), 10),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                ('TOPPADDING', (0, 0), (-1, -1), 6),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
            ]))
            story.append(t)
            story.append(Spacer(1, 10))
        chart_path = os.path.join(CHARTS_DIR, "inter_core_heatmap.png")
        if os.path.exists(chart_path):
            story.append(Image(chart_path, width=6*inch, height=5*inch))

    # Build PDF with watermark
    doc.build(story, onFirstPage=add_watermark, onLaterPages=add_watermark)
    print(f"\n[PDF] Report generated: {pdf_path}")
    return True


def run_all_tests(verbose=False):
    """Run all tests sequentially"""
    print("\n" + "="*70)
    print("RUNNING ALL BENCHMARK TESTS")
    print("="*70)

    ensure_dirs()

    print("\n[Info] Verifying sudo credentials...")
    if verify_sudo_once():
        print("[Info] sudo credentials cached successfully")
    else:
        print("[Info] Running without sudo")

    results = {}
    for test_name in TESTS:
        success = run_test(test_name, verbose)
        results[test_name] = success

    print("\n" + "="*70)
    print("TEST SUMMARY")
    print("="*70)

    passed = sum(1 for v in results.values() if v)
    total = len(results)

    for test_name, success in results.items():
        status = "[PASS]" if success else "[FAIL]"
        print(f"  {status} {TESTS[test_name]['name']}")

    print(f"\nTotal: {passed}/{total} passed")

    # Generate PDF report if dependencies available
    if passed > 0:
        generate_pdf_report()

    return passed == total


def main():
    parser = argparse.ArgumentParser(
        description="Memory Benchmark Report Generator",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument("--test", "-t", type=str,
                        help="Run a single test and generate its report")
    parser.add_argument("--all", "-a", action="store_true",
                        help="Run all tests and generate PDF report")
    parser.add_argument("--list", "-l", action="store_true",
                        help="List all available tests")

    args = parser.parse_args()

    ensure_dirs()

    if args.list:
        list_tests()
    elif args.all:
        run_all_tests()
    elif args.test:
        run_test(args.test)
    else:
        parser.print_help()
        print("\nExamples:")
        print("  python3 generate_report.py --list")
        print("  python3 generate_report.py --all")
        print("  python3 generate_report.py --test cache_hierarchy")


if __name__ == "__main__":
    main()
