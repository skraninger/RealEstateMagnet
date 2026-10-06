<#
.SYNOPSIS
    Review and manage tracked URLs from the URL tracker

.DESCRIPTION
    Provides a CLI interface to the URL tracking system, allowing you to:
    - View statistics about tracked URLs
    - Review failed URLs (4xx errors) that need manual attention
    - View retryable failures (5xx, timeout, network errors)
    - Mark URLs as reviewed with notes
    - Retry specific or all retryable failures

.PARAMETER Action
    Action to perform:
    - stats: Show URL tracking statistics
    - review: Show review queue (unreviewed 4xx failures)
    - retryable: Show retryable failures (5xx, timeout, network errors)
    - mark-reviewed: Mark a URL as reviewed with optional note
    - retry: Reset a specific URL to pending for retry
    - retry-all: Reset all retryable failures to pending

.PARAMETER Url
    URL to mark as reviewed or retry (required for mark-reviewed and retry actions)

.PARAMETER Note
    Note to add when marking as reviewed (optional)

.PARAMETER Limit
    Limit number of results (0 = no limit, default: 0)

.PARAMETER DbPath
    Path to the SQLite database (default: data/communities.db)

.EXAMPLE
    # Show URL tracking statistics
    .\scripts\review-urls.ps1 -Action stats

.EXAMPLE
    # Show review queue (failed URLs)
    .\scripts\review-urls.ps1 -Action review

.EXAMPLE
    # Show first 20 failed URLs
    .\scripts\review-urls.ps1 -Action review -Limit 20

.EXAMPLE
    # Show retryable failures
    .\scripts\review-urls.ps1 -Action retryable

.EXAMPLE
    # Mark URL as reviewed with note
    .\scripts\review-urls.ps1 -Action mark-reviewed -Url "https://example.com/page" -Note "Need login cookies"

.EXAMPLE
    # Retry specific URL
    .\scripts\review-urls.ps1 -Action retry -Url "https://example.com/timeout"

.EXAMPLE
    # Retry all retryable failures
    .\scripts\review-urls.ps1 -Action retry-all

.NOTES
    Requires the URL tracking system to be initialized.
    URLs are tracked in the source_urls table of the SQLite database.
    
    The URL tracking system automatically detects robot-friendly sites and
    routes them through fast headless browser access. Non-robot-friendly
    sites use the vision browser agent with LLM-driven CAPTCHA handling.
#>

param(
    [Parameter(Mandatory=$true)]
    [ValidateSet('stats', 'review', 'retryable', 'mark-reviewed', 'retry', 'retry-all')]
    [string]$Action,

    [Parameter(Mandatory=$false)]
    [string]$Url = "",

    [Parameter(Mandatory=$false)]
    [string]$Note = "",

    [Parameter(Mandatory=$false)]
    [int]$Limit = 0,

    [Parameter(Mandatory=$false)]
    [string]$DbPath = "data/communities.db"
)

$ErrorActionPreference = "Stop"

# Build command arguments
$cmdArgs = @("--$Action")

if ($Url -ne "") {
    if ($Action -eq "mark-reviewed") {
        $cmdArgs += "--mark-reviewed"
        $cmdArgs += $Url
    } elseif ($Action -eq "retry") {
        $cmdArgs += "--retry"
        $cmdArgs += $Url
    }
}

if ($Note -ne "") {
    $cmdArgs += "--note"
    $cmdArgs += $Note
}

if ($Limit -gt 0) {
    $cmdArgs += "--limit"
    $cmdArgs += $Limit
}

if ($DbPath -ne "data/communities.db") {
    $cmdArgs += "--db-path"
    $cmdArgs += $DbPath
}

# Run the Python CLI
try {
    python -m modules.community.url_tracker @cmdArgs
} catch {
    Write-Error "Failed to execute URL tracker: $_"
    exit 1
}
