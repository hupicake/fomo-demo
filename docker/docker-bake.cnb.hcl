target "control-plane" {
  cache-from = FOMO_CACHE_REPOSITORY != "" ? ["type=registry,ref=${FOMO_CACHE_REPOSITORY}:buildcache-control-plane"] : []
  cache-to   = FOMO_CACHE_REPOSITORY != "" ? ["type=registry,ref=${FOMO_CACHE_REPOSITORY}:buildcache-control-plane,mode=max,image-manifest=true,oci-mediatypes=true,ignore-error=true"] : []
}

target "sandbox" {
  cache-from = FOMO_CACHE_REPOSITORY != "" ? ["type=registry,ref=${FOMO_CACHE_REPOSITORY}:buildcache-sandbox"] : []
  cache-to   = FOMO_CACHE_REPOSITORY != "" ? ["type=registry,ref=${FOMO_CACHE_REPOSITORY}:buildcache-sandbox,mode=max,image-manifest=true,oci-mediatypes=true,ignore-error=true"] : []
}

target "web" {
  cache-from = FOMO_CACHE_REPOSITORY != "" ? ["type=registry,ref=${FOMO_CACHE_REPOSITORY}:buildcache-web"] : []
  cache-to   = FOMO_CACHE_REPOSITORY != "" ? ["type=registry,ref=${FOMO_CACHE_REPOSITORY}:buildcache-web,mode=max,image-manifest=true,oci-mediatypes=true,ignore-error=true"] : []
}
