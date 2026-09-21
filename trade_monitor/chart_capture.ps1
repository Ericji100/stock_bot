param(
    [Parameter(Mandatory = $true)]
    [string]$OutputPath,
    [int]$CropX = 8,
    [int]$CropY = 92,
    [int]$CropWidth = 1810,
    [int]$CropHeight = 940,
    [int]$MinimumWindowWidth = 1500,
    [int]$MinimumWindowHeight = 1000,
    [double]$MinimumDarkRatio = 0.55,
    [int]$MinimumSignalSamples = 12
)

$ErrorActionPreference = "Stop"
$captureStage = "initializing"

Add-Type -AssemblyName System.Drawing
Add-Type @'
using System;
using System.Runtime.InteropServices;
using System.Text;

public static class TradeMonitorNativeWindow {
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern bool EnumWindows(EnumWindowsProc callback, IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int maxCount);

    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool IsIconic(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool GetWindowRect(IntPtr hWnd, out Rect rect);

    public struct Rect {
        public int Left;
        public int Top;
        public int Right;
        public int Bottom;
    }
}
'@

function Write-ResultAndExit {
    param(
        [bool]$Ok,
        [string]$Status,
        [hashtable]$Extra = @{},
        [int]$ExitCode = 0
    )

    $payload = [ordered]@{
        ok = $Ok
        status = $Status
    }
    foreach ($key in $Extra.Keys) {
        $payload[$key] = $Extra[$key]
    }
    [Console]::Out.WriteLine(($payload | ConvertTo-Json -Compress -Depth 5))
    exit $ExitCode
}

try {
    $captureStage = "validating_arguments"
    if ($CropX -lt 0 -or $CropY -lt 0 -or $CropWidth -le 0 -or $CropHeight -le 0) {
        throw "Invalid chart crop rectangle"
    }

    $captureStage = "locating_target_window"
    $candidates = New-Object System.Collections.Generic.List[object]
    [TradeMonitorNativeWindow]::EnumWindows({
        param($windowHandle, $lParam)
        if (-not [TradeMonitorNativeWindow]::IsWindowVisible($windowHandle)) {
            return $true
        }
        if ([TradeMonitorNativeWindow]::IsIconic($windowHandle)) {
            return $true
        }

        $titleBuilder = New-Object System.Text.StringBuilder 512
        [void][TradeMonitorNativeWindow]::GetWindowText(
            $windowHandle,
            $titleBuilder,
            $titleBuilder.Capacity
        )
        $title = $titleBuilder.ToString()
        $isChromeRemoteDesktop = $title -like "DESKTOP-* - Google Chrome"
        $isEdgeRemoteDesktop = $title -like "DESKTOP-*Microsoft*Edge*"
        if ($isChromeRemoteDesktop -or $isEdgeRemoteDesktop) {
            $windowRect = New-Object TradeMonitorNativeWindow+Rect
            if ([TradeMonitorNativeWindow]::GetWindowRect($windowHandle, [ref]$windowRect)) {
                $candidates.Add([pscustomobject]@{
                    Handle = $windowHandle
                    Title = $title
                    Rect = $windowRect
                })
            }
        }
        return $true
    }, [IntPtr]::Zero) | Out-Null

    if ($candidates.Count -ne 1) {
        Write-ResultAndExit -Ok $false -Status "target_window_not_unique" -Extra @{
            candidate_count = $candidates.Count
        } -ExitCode 2
    }

    $target = $candidates[0]
    $windowWidth = $target.Rect.Right - $target.Rect.Left
    $windowHeight = $target.Rect.Bottom - $target.Rect.Top
    if ($windowWidth -lt $MinimumWindowWidth -or $windowHeight -lt $MinimumWindowHeight) {
        Write-ResultAndExit -Ok $false -Status "target_window_too_small" -Extra @{
            window_width = $windowWidth
            window_height = $windowHeight
        } -ExitCode 2
    }
    if (($CropX + $CropWidth) -gt $windowWidth -or ($CropY + $CropHeight) -gt $windowHeight) {
        Write-ResultAndExit -Ok $false -Status "crop_out_of_bounds" -Extra @{
            window_width = $windowWidth
            window_height = $windowHeight
        } -ExitCode 2
    }

    $captureStage = "preparing_destination"
    $destination = [System.IO.Path]::GetFullPath($OutputPath)
    $destinationDirectory = [System.IO.Path]::GetDirectoryName($destination)
    [System.IO.Directory]::CreateDirectory($destinationDirectory) | Out-Null

    $captureStage = "capturing_chart"
    $bitmap = New-Object System.Drawing.Bitmap($CropWidth, $CropHeight)
    try {
        $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
        try {
            $graphics.CopyFromScreen(
                $target.Rect.Left + $CropX,
                $target.Rect.Top + $CropY,
                0,
                0,
                $bitmap.Size,
                [System.Drawing.CopyPixelOperation]::SourceCopy
            )
        }
        finally {
            $graphics.Dispose()
        }

        $captureStage = "validating_chart_pixels"
        $capturedAt = [DateTimeOffset]::Now
        $darkSamples = 0
        $signalSamples = 0
        $totalSamples = 0
        for ($sampleY = 0; $sampleY -lt $CropHeight; $sampleY += 8) {
            for ($sampleX = 0; $sampleX -lt $CropWidth; $sampleX += 8) {
                $pixel = $bitmap.GetPixel($sampleX, $sampleY)
                $totalSamples++
                if (($pixel.R + $pixel.G + $pixel.B) -lt 180) {
                    $darkSamples++
                }
                $isRed = $pixel.R -gt 100 -and $pixel.R -gt ($pixel.G + 30)
                $isGreen = $pixel.G -gt 90 -and $pixel.G -gt ($pixel.R + 25)
                $isCyan = $pixel.G -gt 90 -and $pixel.B -gt 80 -and $pixel.R -lt 100
                if ($isRed -or $isGreen -or $isCyan) {
                    $signalSamples++
                }
            }
        }
        $darkRatio = if ($totalSamples -eq 0) { 0.0 } else { $darkSamples / $totalSamples }
        if ($darkRatio -lt $MinimumDarkRatio -or $signalSamples -lt $MinimumSignalSamples) {
            Write-ResultAndExit -Ok $false -Status "chart_visual_validation_failed" -Extra @{
                dark_ratio = [math]::Round($darkRatio, 4)
                signal_samples = $signalSamples
                total_samples = $totalSamples
            } -ExitCode 2
        }

        $captureStage = "saving_chart"
        $temporary = "$destination.$PID.tmp.png"
        try {
            $bitmap.Save($temporary, [System.Drawing.Imaging.ImageFormat]::Png)
            [System.IO.File]::Copy($temporary, $destination, $true)
            [System.IO.File]::Delete($temporary)
        }
        finally {
            if ([System.IO.File]::Exists($temporary)) {
                [System.IO.File]::Delete($temporary)
            }
        }
    }
    finally {
        $bitmap.Dispose()
    }

    Write-ResultAndExit -Ok $true -Status "captured" -Extra @{
        path = $destination
        captured_at = $capturedAt.ToString("o")
        width = $CropWidth
        height = $CropHeight
        dark_ratio = [math]::Round($darkRatio, 4)
        signal_samples = $signalSamples
        source_title = $target.Title
    }
}
catch {
    Write-ResultAndExit -Ok $false -Status "capture_failed" -Extra @{
        error = "Pure chart capture failed"
        stage = $captureStage
    } -ExitCode 2
}
