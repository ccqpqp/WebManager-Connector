param([switch]$Background)
# Portable, single-instance Windows tray supervisor. No admin or external libraries.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()
Add-Type -ReferencedAssemblies @('System.Windows.Forms','System.Drawing') -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class WebManagerNativeIcon {
    [DllImport("user32.dll")]
    public static extern bool DestroyIcon(IntPtr handle);
    [DllImport("user32.dll")]
    public static extern bool ShowWindow(IntPtr handle, int command);
    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr handle);
    [DllImport("user32.dll")]
    public static extern IntPtr SendMessage(IntPtr handle, uint message, IntPtr value, IntPtr data);
    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    public static extern int SetCurrentProcessExplicitAppUserModelID(string appId);
}
public class WebManagerPassiveNotice : System.Windows.Forms.Form {
    protected override bool ShowWithoutActivation { get { return true; } }
    protected override System.Windows.Forms.CreateParams CreateParams {
        get {
            System.Windows.Forms.CreateParams cp=base.CreateParams;
            cp.ExStyle |= 0x08000000 | 0x00000080;
            return cp;
        }
    }
}
'@
function New-WebManagerIcon {
    $bitmap=[System.Drawing.Bitmap]::new(32,32,[System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $graphics=[System.Drawing.Graphics]::FromImage($bitmap)
    $background=[System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(37,99,235))
    $foreground=[System.Drawing.SolidBrush]::new([System.Drawing.Color]::White)
    $font=[System.Drawing.Font]::new('Segoe UI',18,[System.Drawing.FontStyle]::Bold,[System.Drawing.GraphicsUnit]::Pixel)
    $format=[System.Drawing.StringFormat]::new()
    try {
        $graphics.SmoothingMode=[System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
        $graphics.Clear([System.Drawing.Color]::Transparent)
        $graphics.FillEllipse($background,1,1,30,30)
        $format.Alignment=[System.Drawing.StringAlignment]::Center
        $format.LineAlignment=[System.Drawing.StringAlignment]::Center
        $graphics.DrawString('W',$font,$foreground,[System.Drawing.RectangleF]::new(0,-1,32,32),$format)
        $handle=$bitmap.GetHicon()
        try { return [System.Drawing.Icon]::FromHandle($handle).Clone() }
        finally { [void][WebManagerNativeIcon]::DestroyIcon($handle) }
    } finally {
        $format.Dispose()
        $font.Dispose()
        $foreground.Dispose()
        $background.Dispose()
        $graphics.Dispose()
        $bitmap.Dispose()
    }
}
$script:appIcon=New-WebManagerIcon
[void][WebManagerNativeIcon]::SetCurrentProcessExplicitAppUserModelID('ccqpqp.WebManager.Connector')
$created = $false
$script:mutex = [System.Threading.Mutex]::new($true,'Local\WebManagerConnectorTray',[ref]$created)
if (-not $created) {
    if (-not $Background) {
        [void][System.Windows.Forms.MessageBox]::Show('이미 실행 중입니다. 작업 표시줄의 숨겨진 아이콘에서 WebManager 연결창을 열어주세요.','WebManager PC 연결')
    }
    exit 0
}
$script:root = Split-Path -Parent $MyInvocation.MyCommand.Path
$script:agentFile = Join-Path $script:root 'agent.py'
$script:origin = 'https://webmanager-2nv.pages.dev'
$script:dataDir = Join-Path $env:LOCALAPPDATA 'WebManagerConnector'
$script:agentConfig = Join-Path $script:dataDir 'settings.json'
$script:preferences = Join-Path $script:dataDir 'tray-settings.json'
$script:startup = Join-Path ([Environment]::GetFolderPath('Startup')) 'WebManager Connector.lnk'
$script:agent = $null
$script:quitting = $false
$script:failures = 0
$script:busy = $false
$script:pausedUntil = [datetime]::MinValue
$script:restarts = [System.Collections.ArrayList]::new()
$script:cloudChecked = [datetime]::MinValue
$script:cloudState = '웹 확인 전'
$script:recover = $true
$script:notifications = $true
$script:displayFile = Join-Path $script:dataDir 'notification-display.json'
$script:trayStatusFile = Join-Path $script:dataDir 'tray-status.json'
$script:traySourceHash = try {(Get-FileHash -LiteralPath (Join-Path $script:root 'WebManager-Tray.ps1') -Algorithm SHA256).Hash.ToLowerInvariant()}catch{''}
$script:popup = $null
$script:popupTimer = $null
$script:noticeSeenFile = Join-Path $script:dataDir 'notification-seen.json'
$script:noticeSeen = @()
$script:noticeInitialized = (Test-Path -LiteralPath $script:noticeSeenFile)
if ($script:noticeInitialized) {
    try { $script:noticeSeen = @(Get-Content -LiteralPath $script:noticeSeenFile -Raw -Encoding UTF8 | ConvertFrom-Json) }
    catch { $script:noticeSeen = @() }
}
New-Item -Path $script:dataDir -ItemType Directory -Force | Out-Null
if (Test-Path $script:preferences) {
    try {
        $p = Get-Content -LiteralPath $script:preferences -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($null -ne $p.autoRecover) { $script:recover = [bool]$p.autoRecover }
        if ($null -ne $p.deployNotifications) { $script:notifications = [bool]$p.deployNotifications }
    } catch {}
}
function Save-Prefs {
    @{autoRecover=$script:recover;deployNotifications=$script:notifications} | ConvertTo-Json -Compress | Set-Content -LiteralPath $script:preferences -Encoding UTF8
}
function Token {
    try {
        $settings = Get-Content -LiteralPath $script:agentConfig -Raw -Encoding UTF8 | ConvertFrom-Json
        if ([string]$settings.token -match '^[A-Za-z0-9_-]{32,256}$') { return [string]$settings.token }
    } catch {}
    return ''
}
function Request-Agent([string]$route) {
    $token = Token
    if (-not $token) { return $null }
    try {
        return Invoke-RestMethod -Uri ('http://127.0.0.1:18765'+$route) -Method GET -Headers @{'X-WebManager-Connector-Token'=$token} -TimeoutSec 5
    } catch { return $null }
}
function Post-Agent([string]$route) {
    $token = Token
    if (-not $token) { return $null }
    try {
        return Invoke-RestMethod -Uri ('http://127.0.0.1:18765'+$route) -Method POST `
          -Headers @{'X-WebManager-Connector-Token'=$token} -ContentType 'application/json' `
          -Body '{}' -TimeoutSec 8
    } catch { return $null }
}
function Save-NoticeSeen {
    try {
        @($script:noticeSeen | Select-Object -Last 180) | ConvertTo-Json -Compress |
          Set-Content -LiteralPath $script:noticeSeenFile -Encoding UTF8
    } catch {}
}
function Save-Tray-Status {
    try {
        @{at=[datetime]::UtcNow.ToString('o');pid=$PID;fileHash=$script:traySourceHash;
          notificationsEnabled=$script:notifications} | ConvertTo-Json -Compress |
          Set-Content -LiteralPath $script:trayStatusFile -Encoding UTF8
    } catch {}
}
function Save-NoticeDisplay([string]$id,[string]$state,[string]$message) {
    try {
        @{id=$id;state=$state;message=$message;at=[datetime]::UtcNow.ToString('o')} |
          ConvertTo-Json -Compress |
          Set-Content -LiteralPath $script:displayFile -Encoding UTF8
    } catch {}
}
function Close-NoticePopup {
    if ($script:popupTimer) {
        try { $script:popupTimer.Stop();$script:popupTimer.Dispose() } catch {}
        $script:popupTimer=$null
    }
    if ($script:popup) {
        try { $script:popup.Close();$script:popup.Dispose() } catch {}
        $script:popup=$null
    }
}
function Show-Desktop-Notice([string]$id,[string]$title,[string]$message,[bool]$warning=$false) {
    try {
        Close-NoticePopup
        # Passive WinForms desktop notification: visible even when Windows suppresses
        # NotifyIcon balloons; it does not steal keyboard focus or require a module.
        $popup=[WebManagerPassiveNotice]::new()
        $popup.StartPosition=[System.Windows.Forms.FormStartPosition]::Manual
        $popup.FormBorderStyle=[System.Windows.Forms.FormBorderStyle]::None
        $popup.ShowInTaskbar=$false
        $popup.ShowIcon=$false
        $popup.TopMost=$true
        $popup.ClientSize=[System.Drawing.Size]::new(370,108)
        $popup.BackColor=[System.Drawing.Color]::FromArgb(22,30,45)
        $popup.ForeColor=[System.Drawing.Color]::White
        $area=[System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
        $popup.Location=[System.Drawing.Point]::new(
            [Math]::Max(0,$area.Right-$popup.Width-18),
            [Math]::Max(0,$area.Bottom-$popup.Height-18))
        $header=[System.Windows.Forms.Label]::new()
        $header.SetBounds(15,12,322,24)
        $header.ForeColor=if($warning){[System.Drawing.Color]::FromArgb(255,177,177)}else{[System.Drawing.Color]::FromArgb(184,225,255)}
        $header.Font=[System.Drawing.Font]::new('Segoe UI',11,[System.Drawing.FontStyle]::Bold)
        $header.Text=$title
        $messageLabel=[System.Windows.Forms.Label]::new()
        $messageLabel.SetBounds(15,42,337,48)
        $messageLabel.Font=[System.Drawing.Font]::new('Segoe UI',10)
        $messageLabel.Text=$message
        $close=[System.Windows.Forms.Button]::new()
        $close.Text='×'
        $close.SetBounds(339,7,24,24)
        $close.FlatStyle=[System.Windows.Forms.FlatStyle]::Flat
        $close.FlatAppearance.BorderSize=0
        $close.ForeColor=[System.Drawing.Color]::White
        $close.BackColor=$popup.BackColor
        $close.Add_Click({Close-NoticePopup})
        $popup.Controls.AddRange(@($header,$messageLabel,$close))
        $script:popup=$popup
        $popup.Show()
        $timer=[System.Windows.Forms.Timer]::new()
        $timer.Interval=9000
        $timer.Add_Tick({Close-NoticePopup})
        $script:popupTimer=$timer
        $timer.Start()
        Save-NoticeDisplay $id 'shown' 'PC 바탕화면 팝업 표시 요청 완료'
        return $true
    } catch {
        Save-NoticeDisplay $id 'failed' ('화면 표시 실패: '+$_.Exception.Message)
        Close-NoticePopup
        return $false
    }
}
function Check-Deploy-Notices {
    $feed=Request-Agent '/v1/notifications'
    if (-not $feed -or $null -eq $feed.items) { return }
    $items=@($feed.items)
    if (-not $script:noticeInitialized) {
        # Ignore old history on first run, but keep events from the past 3 minutes.
        foreach ($item in $items) {
            $when=[datetime]::MinValue
            try { $when=[datetime]::Parse([string]$item.createdAt).ToUniversalTime() } catch {}
            if ($when -lt [datetime]::UtcNow.AddMinutes(-3)) {
                $script:noticeSeen += [string]$item.id
            }
        }
        $script:noticeInitialized=$true
        Save-NoticeSeen
    }
    $handled=$false
    foreach ($item in $items) {
        $id=[string]$item.id
        if (-not $id -or $id -in $script:noticeSeen) { continue }
        if ($handled) { continue }
        $when=[datetime]::MinValue
        try { $when=[datetime]::Parse([string]$item.createdAt).ToUniversalTime() } catch {}
        if ($when -lt [datetime]::UtcNow.AddMinutes(-30)) {
            $script:noticeSeen += $id
            continue
        }
        $script:noticeSeen += $id
        $handled=$true
        if (-not $script:notifications) {
            Save-NoticeDisplay $id 'disabled' '트레이 배포 알림 설정이 꺼져 있습니다.'
            continue
        }
        $project=[string]$item.project
        $event=[string]$item.event
        $title='WebManager · '+$project
        $message=if ($event -eq 'test') {'알림 테스트입니다. 이 창이 보이면 PC 알림 기능이 정상 작동합니다.'}
        elseif ($event -eq 'success' -and [string]$item.source -eq 'cloudflare') {'Cloudflare 운영 배포 완료'}
        elseif ($event -eq 'success') {'PC 업로드 완료 · 운영 반영 확인 필요'}
        else {'배포 실패 · WebManager 기록 확인'}
        [void](Show-Desktop-Notice $id $title $message ($event -eq 'failure'))
    }
    $script:noticeSeen=@($script:noticeSeen | Select-Object -Last 180)
    Save-NoticeSeen
}
function Update-Status([string]$a,[string]$b) {
    $script:statusLabel.Text=$a
    $script:detailLabel.Text=$b
    $name='WebManager - '+$a
    $script:tray.Text=$name.Substring(0,[Math]::Min(63,$name.Length))
}
function Cloud-Health {
    if (((Get-Date)-$script:cloudChecked).TotalSeconds -lt 120) { return }
    $script:cloudChecked=Get-Date
    try {
        $request=[System.Net.HttpWebRequest]::Create($script:origin)
        $request.Method='HEAD'
        $request.Timeout=3000
        $response=$request.GetResponse()
        $script:cloudState='웹 접속 가능'
        $response.Close()
    } catch [System.Net.WebException] {
        if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -lt 500) {
            $script:cloudState='웹 접속 가능'
            $_.Exception.Response.Close()
        } else { $script:cloudState='웹 접속 불가' }
    } catch { $script:cloudState='웹 접속 불가' }
}
function Start-Agent {
    $py=Get-Command py.exe -ErrorAction SilentlyContinue
    if (-not $py) { Update-Status 'Python 3 필요' 'Python 3 설치 후 다시 실행하세요.'; return $false }
    if (-not (Test-Path $script:agentFile)) { Update-Status '연결 파일 없음' 'ZIP의 파일을 같은 폴더에 압축 해제하세요.'; return $false }
    try {
        $args='-3 "'+$script:agentFile+'" --quick-start --no-browser'
        $script:agent=Start-Process -FilePath $py.Source -ArgumentList $args -WorkingDirectory $script:root -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $script:dataDir 'agent-output.log') -RedirectStandardError (Join-Path $script:dataDir 'agent-errors.log')
        return $true
    } catch {
        Update-Status '연결 시작 실패' $_.Exception.Message
        return $false
    }
}
function Stop-Owned-Agent {
    if (-not $script:agent) { return }
    try {
        $script:agent.Refresh()
        if (-not $script:agent.HasExited) {
            & taskkill.exe /PID $script:agent.Id /T /F 1>$null 2>$null
        }
    } catch {
        try { if (-not $script:agent.HasExited) { $script:agent.Kill() } } catch {}
    }
    $script:agent=$null
}
function Ensure-Agent([switch]$Force) {
    $reply=Request-Agent '/v1/status'
    if ($reply -and $reply.connected) {
        if ([string]$reply.version -ne '0.7.0') {
            Update-Status '이전 연결 프로그램 실행 중' '예전 실행.cmd 창을 종료한 뒤 연결 복구를 누르세요.'
            return $false
        }
        $script:failures=0
        return $true
    }
    $script:failures++
    if (-not $Force -and (-not $script:recover -or $script:failures -lt 4)) { return $false }
    $now=Get-Date
    if (-not $Force -and $now -lt $script:pausedUntil) { return $false }
    foreach ($t in @($script:restarts)) {
        if (($now-$t).TotalMinutes -gt 10) { [void]$script:restarts.Remove($t) }
    }
    if (-not $Force -and $script:restarts.Count -ge 3) {
        $script:pausedUntil=$now.AddMinutes(10)
        Update-Status '자동 복구 일시정지' '재시작이 반복돼 10분 후 다시 시도합니다.'
        return $false
    }
    Stop-Owned-Agent
    [void]$script:restarts.Add($now)
    $script:failures=0
    return Start-Agent
}
function Open-Web {
    if (-not (Request-Agent '/v1/status')) { [void](Ensure-Agent -Force); Start-Sleep -Milliseconds 900 }
    $key=Token
    if (-not $key -or -not (Request-Agent '/v1/status')) {
        Update-Status 'PC 연결 불가' 'Python 및 오류 기록을 확인하세요.'
        return
    }
    try {
        $start=[System.Diagnostics.ProcessStartInfo]::new($script:origin+'/#wm-connector='+$key)
        $start.UseShellExecute=$true
        [void][System.Diagnostics.Process]::Start($start)
    } catch { Update-Status '브라우저 열기 실패' '웹매니저를 브라우저에서 열어주세요.' }
}
function Start-ConnectorUpdate {
    if (-not (Request-Agent '/v1/status')) { [void](Ensure-Agent -Force); Start-Sleep -Milliseconds 900 }
    $check=Request-Agent '/v1/update/check'
    if (-not $check) {
        Update-Status '업데이트 확인 실패' '인터넷 연결을 확인한 뒤 다시 눌러주세요.'
        return
    }
    if (-not $check.available -and -not $check.restartNeeded) {
        Update-Status '최신 버전' '현재 프로그램이 최신 상태입니다.'
        return
    }
    $updateButton.Enabled=$false
    Update-Status '업데이트 시작' '최신 파일을 확인하고 설치한 뒤 자동으로 다시 시작합니다.'
    if (-not (Post-Agent '/v1/update/apply')) {
        $updateButton.Enabled=$true
        Update-Status '업데이트 시작 실패' '잠시 후 다시 눌러주세요.'
    }
}
function Startup-Enabled([bool]$enable) {
    if ($enable) {
        $wsh=New-Object -ComObject WScript.Shell
        $link=$wsh.CreateShortcut($script:startup)
        $link.TargetPath=Join-Path $env:SystemRoot 'System32\wscript.exe'
        $link.Arguments='//B "'+(Join-Path $script:root 'WebManager-Start.vbs')+'" background'
        $link.WorkingDirectory=$script:root
        $link.Description='WebManager PC 연결'
        $link.Save()
    } elseif (Test-Path $script:startup) { Remove-Item -LiteralPath $script:startup -Force }
}
function Show-Window {
    $script:form.ShowInTaskbar=$true
    $script:form.Show()
    $script:form.WindowState=[System.Windows.Forms.FormWindowState]::Normal
    [void][WebManagerNativeIcon]::SendMessage($script:form.Handle,0x0080,[IntPtr]0,$script:appIcon.Handle)
    [void][WebManagerNativeIcon]::SendMessage($script:form.Handle,0x0080,[IntPtr]1,$script:appIcon.Handle)
    [void][WebManagerNativeIcon]::ShowWindow($script:form.Handle,9)
    [void][WebManagerNativeIcon]::SetForegroundWindow($script:form.Handle)
    $script:form.Activate()
}
function Exit-App {
    $script:quitting=$true
    $script:watch.Stop()
    Stop-Owned-Agent
    $script:tray.Visible=$false
    $script:tray.Dispose()
    $script:form.Close()
    [System.Windows.Forms.Application]::Exit()
}
$script:form=New-Object System.Windows.Forms.Form
$script:form.Text='WebManager PC 연결'
$script:form.ClientSize=[System.Drawing.Size]::new(442,207)
$script:form.StartPosition='CenterScreen'
$script:form.FormBorderStyle='FixedDialog'
$script:form.MaximizeBox=$false
$script:form.ShowInTaskbar=(-not $Background)
$script:form.Icon=$script:appIcon
$script:form.Font=[System.Drawing.Font]::new('Segoe UI',9)
$script:statusLabel=New-Object System.Windows.Forms.Label
$script:statusLabel.SetBounds(14,14,337,24)
$script:statusLabel.Font=[System.Drawing.Font]::new('Segoe UI',11,[System.Drawing.FontStyle]::Bold)
$script:statusLabel.Text='PC 연결 준비 중'
$script:detailLabel=New-Object System.Windows.Forms.Label
$script:detailLabel.SetBounds(14,43,415,28)
$script:detailLabel.Text='로컬 연결과 PC 상태를 확인합니다.'
$script:versionLabel=New-Object System.Windows.Forms.Label
$script:versionLabel.SetBounds(14,76,412,18)
$script:versionLabel.Font=[System.Drawing.Font]::new('Segoe UI',8)
$script:versionLabel.ForeColor=[System.Drawing.Color]::FromArgb(98,110,125)
$packageVersion=try {
    $value=(Get-Content -LiteralPath (Join-Path $script:root 'connector-version.json') -Raw -Encoding UTF8 | ConvertFrom-Json).hash
    if ([string]$value -match '^[a-f0-9]{64}$') {[string]$value}else{''}
} catch {''}
$script:versionLabel.Text=if ($packageVersion.Length -ge 12) {
    '설치 버전 · '+$packageVersion.Substring(0,12)
} else { '설치 버전 · 확인 불가' }
$script:installLabel=New-Object System.Windows.Forms.Label
$script:installLabel.SetBounds(14,104,414,37)
$script:installLabel.Font=[System.Drawing.Font]::new('Segoe UI',8)
$script:installLabel.ForeColor=[System.Drawing.Color]::FromArgb(60,75,99)
$script:installLabel.AutoEllipsis=$true
$script:installLabel.Text='프로그램 설치 폴더 · '+$script:root
$script:installTip=New-Object System.Windows.Forms.ToolTip
$script:installTip.SetToolTip($script:installLabel,$script:root)
$script:form.Controls.AddRange(@($script:statusLabel,$script:detailLabel,$script:versionLabel,$script:installLabel))
$connect=New-Object System.Windows.Forms.Button
$connect.Text='웹에서 연결'
$connect.SetBounds(14,157,100,35)
$connect.Add_Click({ Open-Web })
$updateButton=New-Object System.Windows.Forms.Button
$updateButton.Text='업데이트'
$updateButton.SetBounds(120,157,100,35)
$updateButton.Add_Click({ Start-ConnectorUpdate })
$restart=New-Object System.Windows.Forms.Button
$restart.Text='연결 복구'
$restart.SetBounds(326,157,102,35)
$restart.Add_Click({ $script:pausedUntil=[datetime]::MinValue; [void](Ensure-Agent -Force) })
$openFolder=New-Object System.Windows.Forms.Button
$openFolder.Text='설치 폴더 열기'
$openFolder.SetBounds(226,157,94,35)
$openFolder.Add_Click({
    try {Start-Process -FilePath explorer.exe -ArgumentList ('"'+$script:root+'"')}
    catch {Update-Status '설치 폴더 열기 실패' $_.Exception.Message}
})
$script:form.Controls.AddRange(@($connect,$updateButton,$openFolder,$restart))
$settingsButton=New-Object System.Windows.Forms.Button
$settingsButton.Text='설정'
$settingsButton.SetBounds(368,10,60,29)
$script:form.Controls.Add($settingsButton)
$script:form.Add_FormClosing({ if (-not $script:quitting) { $_.Cancel=$true; $script:form.Hide() } })
function Open-Settings {
    $dlg=New-Object System.Windows.Forms.Form
    $dlg.Text='WebManager · PC 연결 설정'
    $dlg.ClientSize=[System.Drawing.Size]::new(430,320)
    $dlg.FormBorderStyle='FixedDialog'
    $dlg.StartPosition='CenterParent'
    $dlg.MaximizeBox=$false
    $dlg.Font=[System.Drawing.Font]::new('Segoe UI',9)
    $startupBox=New-Object System.Windows.Forms.CheckBox
    $startupBox.SetBounds(16,13,390,28)
    $startupBox.Text='Windows 로그인 시 자동 시작'
    $startupBox.Checked=Test-Path $script:startup
    $startupBox.Add_CheckedChanged({
        try {
            Startup-Enabled $startupBox.Checked
            $script:menuStartup.Checked=$startupBox.Checked
        } catch {
            $startupBox.Checked=Test-Path $script:startup
            [void][System.Windows.Forms.MessageBox]::Show($_.Exception.Message,'설정 저장 실패')
        }
    })
    $recoverBox=New-Object System.Windows.Forms.CheckBox
    $recoverBox.SetBounds(16,44,390,28)
    $recoverBox.Text='연결 프로그램 자동 복구 (재시작 횟수 제한)'
    $recoverBox.Checked=$script:recover
    $recoverBox.Add_CheckedChanged({
        $script:recover=$recoverBox.Checked
        $script:menuRecovery.Checked=$recoverBox.Checked
        Save-Prefs
    })
    $info=New-Object System.Windows.Forms.TextBox
    $info.Multiline=$true
    $info.ReadOnly=$true
    $info.ScrollBars='Vertical'
    $info.SetBounds(16,82,397,152)
    $info.BackColor=[System.Drawing.Color]::FromArgb(239,243,249)
    $health=Request-Agent '/v1/system/status'
    $jobs=Request-Agent '/v1/deploy/status'
    $agentStatus=Request-Agent '/v1/status'
    $cpu=if ($health -and $null -ne $health.cpuPercent) { [string]$health.cpuPercent+'%' } else { '확인 불가' }
    $ram=if ($health -and $null -ne $health.ramPercent) { [string]$health.ramPercent+'%' } else { '확인 불가' }
    $uptime=if ($health -and $null -ne $health.pcUptimeSec) { [string][Math]::Floor($health.pcUptimeSec/3600)+'시간' } else { '확인 불가' }
    $queued=if ($jobs) { [string]$jobs.queued+'건' } else { '조회 불가' }
    $running=if ($jobs) { [string]$jobs.activeCount+' / '+[string]$jobs.maxConcurrent+'개' } else { '조회 불가' }
    $auto=if ($jobs -and $jobs.autoMonitor) {
        $monitor=$jobs.autoMonitor
        if (-not $monitor.running) { '중지' }
        elseif ($monitor.lastError) { '설정 조회 오류' }
        else { 'PC 백그라운드 감시 중 ('+[string]@($monitor.enabled).Count+'개)' }
    } else { '확인 불가' }
    $version=if ($agentStatus) { [string]$agentStatus.version } else { '응답 없음' }
    $info.Lines=@(
      ('PC 연결: '+$(if ($health -and $health.pcOnline) {'정상'} else {'확인 불가'})),
      ('연결 프로그램: '+$version),
      ('CPU: '+$cpu+'   RAM: '+$ram),
      ('PC 가동 시간: '+$uptime),
      ('로컬 배포 대기: '+$queued+'   실행 중: '+$running),
      ('로컬 자동배포: '+$auto),
      ('웹 사이트 연결: '+$script:cloudState),
      ('상태 확인: 30초 간격 / 웹 서버 확인: 2분 간격'),
      ('오류 기록: '+(Join-Path $script:dataDir 'agent-errors.log'))
    )
    $logs=New-Object System.Windows.Forms.Button
    $logs.Text='기록 폴더'
    $logs.SetBounds(16,256,96,34)
    $logs.Add_Click({ [System.Diagnostics.Process]::Start('explorer.exe',$script:dataDir) | Out-Null })
    $stopAll=New-Object System.Windows.Forms.Button
    $stopAll.Text='관련 프로그램 전체 종료'
    $stopAll.SetBounds(187,256,226,34)
    $stopAll.Add_Click({
        $answer=[System.Windows.Forms.MessageBox]::Show(
          '이 트레이가 시작한 연결 프로그램과 진행 중인 로컬 배포를 종료할까요? 다른 개발 프로그램은 종료하지 않습니다.',
          'WebManager 관련 프로그램 종료',[System.Windows.Forms.MessageBoxButtons]::YesNo,[System.Windows.Forms.MessageBoxIcon]::Warning)
        if ($answer -eq [System.Windows.Forms.DialogResult]::Yes) {
            $dlg.Close()
            Exit-App
        }
    })
    $dlg.Controls.AddRange(@($startupBox,$recoverBox,$info,$logs,$stopAll))
    try { [void]$dlg.ShowDialog($script:form) } finally { $dlg.Dispose() }
}
$settingsButton.Add_Click({ Open-Settings })
$script:tray=New-Object System.Windows.Forms.NotifyIcon
$script:tray.Icon=$script:appIcon
$script:tray.Visible=$true
$script:tray.Text='WebManager PC 연결'
$menu=New-Object System.Windows.Forms.ContextMenuStrip
$openItem=$menu.Items.Add('연결창 열기')
$openItem.Add_Click({ Show-Window })
$connectItem=$menu.Items.Add('웹에서 연결')
$connectItem.Add_Click({ Open-Web })
$restartItem=$menu.Items.Add('연결 복구')
$restartItem.Add_Click({ $script:pausedUntil=[datetime]::MinValue; [void](Ensure-Agent -Force) })
[void]$menu.Items.Add('-')
$script:menuRecovery=New-Object System.Windows.Forms.ToolStripMenuItem('자동 복구')
$script:menuRecovery.CheckOnClick=$true
$script:menuRecovery.Checked=$script:recover
$script:menuRecovery.Add_CheckedChanged({ $script:recover=$script:menuRecovery.Checked; Save-Prefs })
[void]$menu.Items.Add($script:menuRecovery)
$script:menuStartup=New-Object System.Windows.Forms.ToolStripMenuItem('Windows 시작 시 실행')
$script:menuStartup.CheckOnClick=$true
$script:menuStartup.Checked=Test-Path $script:startup
$script:menuStartup.Add_CheckedChanged({
    try { Startup-Enabled $script:menuStartup.Checked }
    catch { $script:menuStartup.Checked=Test-Path $script:startup }
})
[void]$menu.Items.Add($script:menuStartup)
$script:menuNotifications=New-Object System.Windows.Forms.ToolStripMenuItem('배포 완료·실패 윈도우 알림')
$script:menuNotifications.CheckOnClick=$true
$script:menuNotifications.Checked=$script:notifications
$script:menuNotifications.Add_CheckedChanged({
    $script:notifications=$script:menuNotifications.Checked
    Save-Prefs
})
[void]$menu.Items.Add($script:menuNotifications)
$testNotice=$menu.Items.Add('배포 알림 테스트')
$testNotice.Add_Click({
    [void](Show-Desktop-Notice 'tray-manual-test' 'WebManager · 알림 테스트' 'PC 배포 알림 표시 테스트입니다.' $false)
})
[void]$menu.Items.Add('-')
$settingsItem=$menu.Items.Add('설정 · PC 정보')
$settingsItem.Add_Click({ Show-Window; Open-Settings })
[void]$menu.Items.Add('-')
$quitItem=$menu.Items.Add('종료')
$quitItem.Add_Click({ Exit-App })
$script:tray.ContextMenuStrip=$menu
$script:tray.Add_DoubleClick({ Show-Window })
$script:watch=New-Object System.Windows.Forms.Timer
$script:watch.Interval=1500
$script:watch.Add_Tick({
    $script:watch.Interval=10000
    if ($script:busy -or $script:quitting) { return }
    $script:busy=$true
    try {
        Save-Tray-Status
        $health=Request-Agent '/v1/system/status'
        if ($health -and $health.agentRunning) {
            $script:failures=0
            $cpu=if ($null -eq $health.cpuPercent) {'--'} else {[string]([Math]::Round($health.cpuPercent))}
            $ram=if ($null -eq $health.ramPercent) {'--'} else {[string]([Math]::Round($health.ramPercent))}
            Cloud-Health
            Update-Status 'PC · 연결 프로그램 정상' ('CPU '+$cpu+'% | RAM '+$ram+'% | '+$script:cloudState)
            Check-Deploy-Notices
        } elseif ((Request-Agent '/v1/status') -and -not $health) {
            Update-Status '이전 버전 또는 상태 조회 오류' '옛 연결 프로그램을 종료한 뒤 연결 복구를 누르세요.'
        } elseif ((Test-Path (Join-Path $script:dataDir 'connector-update-progress.json')) -and
                  ((Get-Content -LiteralPath (Join-Path $script:dataDir 'connector-update-progress.json') -Raw -ErrorAction SilentlyContinue | ConvertFrom-Json).phase -eq 'restarting') -and
                  (Ensure-Agent -Force)) {
            Update-Status '연결 프로그램 업데이트 완료' 'PC 연결을 복구하고 있습니다.'
        } elseif (Ensure-Agent) {
            Update-Status '연결 프로그램 실행 중' '연결 응답을 기다리는 중입니다.'
        } else {
            Update-Status 'PC 켜짐 · 연결 프로그램 응답 없음' '자동 복구를 확인 중입니다. 오류가 반복되면 기록을 확인하세요.'
        }
    } catch { Update-Status '상태 확인 실패' $_.Exception.Message }
    finally { $script:busy=$false }
})
$script:watch.Start()
Save-Tray-Status
[void](Ensure-Agent -Force)
$script:form.Add_Shown({
    if ($Background) { $script:form.Hide() }
    else { [void]$script:form.BeginInvoke([System.Action]{ Show-Window }) }
})
[System.Windows.Forms.Application]::Run($script:form)
Close-NoticePopup
$script:tray.Visible=$false
$script:tray.Dispose()
$script:mutex.ReleaseMutex()
$script:mutex.Dispose()
$script:appIcon.Dispose()
