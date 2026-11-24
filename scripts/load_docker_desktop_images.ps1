$ErrorActionPreference = 'Stop'

$context = (& kubectl config current-context).Trim()
if ($LASTEXITCODE -ne 0 -or $context -ne 'docker-desktop') {
    throw "Expected kubectl context 'docker-desktop'; found '$context'."
}

$archiveName = ".pixelforge-k8s-images-$PID.tar"
$archivePath = Join-Path (Get-Location) $archiveName
$nodeNames = @((& kubectl get nodes -o jsonpath='{.items[*].metadata.name}') -split '\s+' | Where-Object { $_ })
if ($LASTEXITCODE -ne 0 -or $nodeNames.Count -eq 0) {
    throw 'Could not find nodes in the active Kubernetes cluster.'
}

$debugPods = @()
try {
    & docker save -o $archivePath image-api:1.0 image-worker:1.0
    if ($LASTEXITCODE -ne 0) { throw 'docker save failed.' }

    foreach ($nodeName in $nodeNames) {
        $debugOutput = (& kubectl debug "node/$nodeName" --image=busybox:1.36 --profile=sysadmin -- /bin/sh -c 'sleep 900' 2>&1 | Out-String)
        if ($LASTEXITCODE -ne 0) { throw "Could not create image-loader pod on $nodeName`: $debugOutput" }
        if ($debugOutput -notmatch 'Creating debugging pod ([a-z0-9-]+)') {
            throw "Could not identify the image-loader pod: $debugOutput"
        }
        $podName = $Matches[1]
        $debugPods += $podName

        & kubectl wait --for=condition=Ready "pod/$podName" -n default --timeout=90s
        if ($LASTEXITCODE -ne 0) { throw "Image-loader pod $podName did not become ready." }

        & kubectl cp ".\$archiveName" "default/${podName}:/host/tmp/pixelforge-k8s-images.tar"
        if ($LASTEXITCODE -ne 0) { throw "Could not copy images to node $nodeName." }

        & kubectl exec -n default $podName -- chroot /host ctr -n k8s.io images import /tmp/pixelforge-k8s-images.tar
        if ($LASTEXITCODE -ne 0) { throw "Could not import images on node $nodeName." }

        & kubectl exec -n default $podName -- chroot /host rm -f /tmp/pixelforge-k8s-images.tar
    }
}
finally {
    foreach ($podName in $debugPods) {
        & kubectl delete pod $podName -n default --ignore-not-found | Out-Null
    }
    if (Test-Path -LiteralPath $archivePath) {
        Remove-Item -LiteralPath $archivePath
    }
}
