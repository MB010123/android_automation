package com.mobirent.companion.esim

import com.google.common.truth.Truth.assertThat
import org.junit.Test

class EuiccCallbackResultTest {
    @Test
    fun extraZeroIsSuccessEvenWhenBroadcastIsActivityOk() {
        val parsed = EuiccCallbackResult.parse(
            hasResultExtra = true,
            extraValue = 0,
            broadcastResultCode = -1,
        )
        assertThat(parsed.resultCode).isEqualTo(0)
        assertThat(parsed.source).isEqualTo("extra")
        assertThat(parsed.isOk).isTrue()
    }

    @Test
    fun missingExtraDoesNotDefaultToMinusOne() {
        val parsed = EuiccCallbackResult.parse(
            hasResultExtra = false,
            extraValue = -1,
            broadcastResultCode = null,
        )
        assertThat(parsed.resultCode).isNull()
        assertThat(parsed.source).isEqualTo("missing")
        assertThat(parsed.isOk).isFalse()
        assertThat(parsed.isError).isFalse()
    }

    @Test
    fun missingExtraUsesBroadcastZeroAsEuiccOk() {
        val parsed = EuiccCallbackResult.parse(
            hasResultExtra = false,
            extraValue = -1,
            broadcastResultCode = 0,
        )
        assertThat(parsed.resultCode).isEqualTo(0)
        assertThat(parsed.source).isEqualTo("broadcast")
        assertThat(parsed.isOk).isTrue()
    }

    @Test
    fun activityResultOkIsNotTreatedAsEuiccSuccess() {
        val parsed = EuiccCallbackResult.parse(
            hasResultExtra = false,
            extraValue = -1,
            broadcastResultCode = -1,
        )
        assertThat(parsed.resultCode).isNull()
        assertThat(parsed.source).isEqualTo("missing")
    }

    @Test
    fun resolvableAndErrorExtrasArePreserved() {
        val resolvable = EuiccCallbackResult.parse(true, 1, 0)
        assertThat(resolvable.isResolvable).isTrue()
        val error = EuiccCallbackResult.parse(true, 2, 0)
        assertThat(error.isError).isTrue()
        assertThat(error.resultCode).isEqualTo(2)
    }
}
