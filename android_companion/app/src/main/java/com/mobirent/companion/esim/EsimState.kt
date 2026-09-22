package com.mobirent.companion.esim

enum class EsimState(val wireValue: String) {
    READY("ready"),
    PROVISIONING("provisioning"),
    INSTALLED("installed"),
    FAILED("failed"),
    UNSUPPORTED("unsupported"),
    REQUIRES_USER_ACTION("requires_user_action"),
    DRY_RUN("dry_run"),
}
