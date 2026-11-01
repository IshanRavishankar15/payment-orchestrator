param(
    [string]$ApiBaseUrl = "http://127.0.0.1:8000",
    [int]$TimeoutSeconds = 60
)

$ErrorActionPreference = "Stop"

function Get-LocalSetting {
    param([string]$Name)

    $value = [Environment]::GetEnvironmentVariable($Name)
    if (-not [string]::IsNullOrWhiteSpace($value)) {
        return $value
    }

    $envFile = Join-Path (Get-Location) ".env"
    if (Test-Path $envFile) {
        foreach ($line in Get-Content $envFile) {
            if ($line -match "^\s*$([regex]::Escape($Name))\s*=\s*(.*?)\s*$") {
                return $Matches[1].Trim().Trim('"').Trim("'")
            }
        }
    }
    return $null
}

function New-SimulatedPayment {
    param(
        [string]$Scenario,
        [string]$AdminToken,
        [string]$RunId
    )

    $payload = @{
        merchant_id = "payment-orchestrator-demo"
        amount_minor = 1250
        currency = "INR"
        idempotency_key = "phase7-$Scenario-$RunId"
        scenario = $Scenario
        provider_name = "provider_a"
    } | ConvertTo-Json -Compress

    return Invoke-RestMethod `
        -Uri "$ApiBaseUrl/simulation/payments" `
        -Method Post `
        -ContentType "application/json" `
        -Headers @{ "X-Simulation-Admin-Token" = $AdminToken } `
        -Body $payload
}

function Wait-ForPaymentState {
    param(
        [string]$PaymentId,
        [string]$ExpectedState
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $payment = Invoke-RestMethod -Uri "$ApiBaseUrl/payments/$PaymentId"
        if ($payment.state -eq $ExpectedState) {
            return $payment
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)

    throw "Payment $PaymentId did not reach $ExpectedState within $TimeoutSeconds seconds; last state was $($payment.state)."
}

function Get-Signature {
    param(
        [string]$Body,
        [string]$Secret
    )

    $key = [Text.Encoding]::UTF8.GetBytes($Secret)
    $bytes = [Text.Encoding]::UTF8.GetBytes($Body)
    $hmac = [Security.Cryptography.HMACSHA256]::new($key)
    try {
        $digest = $hmac.ComputeHash($bytes)
    } finally {
        $hmac.Dispose()
    }
    return "sha256=" + [BitConverter]::ToString($digest).Replace("-", "").ToLowerInvariant()
}

$adminToken = Get-LocalSetting "SIMULATION_ADMIN_TOKEN"
$callbackSecret = Get-LocalSetting "PROVIDER_A_CALLBACK_SECRET"
if ([string]::IsNullOrWhiteSpace($adminToken)) {
    throw "Set SIMULATION_ADMIN_TOKEN in .env or the process environment before running the demo."
}
if ([string]::IsNullOrWhiteSpace($callbackSecret)) {
    throw "Set PROVIDER_A_CALLBACK_SECRET in .env or the process environment before running the demo."
}

$null = Invoke-RestMethod -Uri "$ApiBaseUrl/ready"
$runId = [Guid]::NewGuid().ToString("N")

$success = New-SimulatedPayment "SUCCESS" $adminToken $runId
$null = Wait-ForPaymentState $success.id "SUCCEEDED"
Write-Output "Success confirmed: $($success.id)"

$decline = New-SimulatedPayment "DECLINE" $adminToken $runId
$null = Wait-ForPaymentState $decline.id "FAILED"
Write-Output "Definitive decline recorded: $($decline.id)"

$lostResponse = New-SimulatedPayment "SUCCESS_LOST_RESPONSE" $adminToken $runId
$null = Wait-ForPaymentState $lostResponse.id "SUCCEEDED"
$reconciledTimeline = Invoke-RestMethod `
    -Uri "$ApiBaseUrl/payments/$($lostResponse.id)/timeline"
$reconciliation = $reconciledTimeline.reconciliation_jobs |
    Select-Object -First 1
if ($null -eq $reconciliation -or $reconciliation.status -ne "COMPLETED") {
    throw "The lost-response payment did not complete reconciliation."
}
Write-Output "Lost response reconciled to success: $($lostResponse.id)"

$uncertain = New-SimulatedPayment "TIMEOUT" $adminToken $runId
$null = Wait-ForPaymentState $uncertain.id "UNKNOWN"
$timeline = Invoke-RestMethod -Uri "$ApiBaseUrl/payments/$($uncertain.id)/timeline"
$providerReference = $timeline.attempts[0].provider_reference
if ([string]::IsNullOrWhiteSpace($providerReference)) {
    throw "The uncertain payment has no provider reference."
}

$callback = @{
    provider_event_id = "phase7-demo-$runId"
    event_type = "PAYMENT_SUCCEEDED"
    provider_reference = $providerReference
} | ConvertTo-Json -Compress
$signature = Get-Signature $callback $callbackSecret
$callbackUri = "$ApiBaseUrl/simulation/providers/provider_a/callbacks"
$headers = @{ "X-Provider-Signature" = $signature }
$firstDelivery = Invoke-RestMethod `
    -Uri $callbackUri `
    -Method Post `
    -ContentType "application/json" `
    -Headers $headers `
    -Body $callback
$duplicateDelivery = Invoke-RestMethod `
    -Uri $callbackUri `
    -Method Post `
    -ContentType "application/json" `
    -Headers $headers `
    -Body $callback

if ($firstDelivery.status -ne "contradictory") {
    throw "Expected the callback without provider confirmation to be contradictory; received $($firstDelivery.status)."
}
if ($duplicateDelivery.status -ne "duplicate") {
    throw "Expected the repeated callback to be deduplicated; received $($duplicateDelivery.status)."
}

$finalTimeline = Invoke-RestMethod `
    -Uri "$ApiBaseUrl/payments/$($uncertain.id)/timeline"
if ($finalTimeline.payment.state -ne "UNKNOWN") {
    throw "The contradictory callback changed the uncertain payment state to $($finalTimeline.payment.state)."
}
$event = $finalTimeline.provider_events |
    Where-Object { $_.provider_event_id -eq "phase7-demo-$runId" } |
    Select-Object -First 1
if ($null -eq $event -or $event.processing_result -ne "CONTRADICTORY") {
    throw "The callback event was not recorded as contradictory in the payment timeline."
}

Write-Output "Uncertain outcome remains UNKNOWN: $($uncertain.id)"
Write-Output "Signed callback recorded as contradictory; duplicate delivery deduplicated."
Write-Output "Inspect the payment timelines in the dashboard at http://127.0.0.1:3000/."
