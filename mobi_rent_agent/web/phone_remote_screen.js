/**
 * Interactive GADS phone surface for the customer rental page.
 *
 * Do not bind pointer events to a swapping MJPEG image element. Reloading
 * the bitmap each frame cancels pointer capture. Paint frames onto a canvas
 * and capture pointers on a stable overlay instead.
 *
 * Browser → POST /rentals/{rental_id}/remote-access/control (tap|swipe|type|back)
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.PhoneRemoteScreen = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  var ALLOWED = { tap: 1, swipe: 1, type: 1, back: 1 };
  var FORBIDDEN = {
    home: 1,
    recents: 1,
    notification: 1,
    keyevent: 1,
    adb: 1,
    shell: 1,
  };
  var SWIPE_THRESHOLD_PX = 12;

  function mapDisplayToDevice(clientX, clientY, rect, deviceW, deviceH) {
    if (deviceW <= 0 || deviceH <= 0 || rect.width <= 0 || rect.height <= 0) return null;
    var dx = clientX - rect.left;
    var dy = clientY - rect.top;
    var scale = Math.min(rect.width / deviceW, rect.height / deviceH);
    var contentW = deviceW * scale;
    var contentH = deviceH * scale;
    var padX = (rect.width - contentW) / 2;
    var padY = (rect.height - contentH) / 2;
    if (dx < padX || dy < padY || dx > padX + contentW || dy > padY + contentH) return null;
    var x = Math.round((dx - padX) / scale);
    var y = Math.round((dy - padY) / scale);
    return {
      x: Math.max(0, Math.min(deviceW - 1, x)),
      y: Math.max(0, Math.min(deviceH - 1, y)),
    };
  }

  function classifyGesture(start, end) {
    var dx = end.x - start.x;
    var dy = end.y - start.y;
    return Math.hypot(dx, dy) >= SWIPE_THRESHOLD_PX ? "swipe" : "tap";
  }

  function PhoneRemoteController(send) {
    this._send = send;
    this.sessionConnected = false;
    this.streamReady = false;
    this.deviceWidth = 0;
    this.deviceHeight = 0;
    this.indicator = null;
    this._press = null;
  }

  PhoneRemoteController.prototype.ready = function () {
    return !!(this.sessionConnected && this.streamReady && this.deviceWidth && this.deviceHeight);
  };

  PhoneRemoteController.prototype.onFrame = function (width, height) {
    if (width > 0 && height > 0) {
      this.deviceWidth = width;
      this.deviceHeight = height;
      this.streamReady = true;
    }
  };

  PhoneRemoteController.prototype.setSessionConnected = function (connected) {
    this.sessionConnected = !!connected;
    if (!connected) {
      this._press = null;
      this.indicator = null;
    }
  };

  PhoneRemoteController.prototype.pointerDown = function (clientX, clientY, rect) {
    if (!this.ready()) {
      this._press = null;
      return false;
    }
    var mapped = mapDisplayToDevice(clientX, clientY, rect, this.deviceWidth, this.deviceHeight);
    if (!mapped) {
      this._press = null;
      return false;
    }
    this._press = { display: { x: clientX, y: clientY }, device: mapped };
    this.indicator = { x: clientX - rect.left, y: clientY - rect.top };
    return true;
  };

  PhoneRemoteController.prototype.pointerMove = function (clientX, clientY, rect) {
    if (!this._press) return;
    this.indicator = { x: clientX - rect.left, y: clientY - rect.top };
  };

  PhoneRemoteController.prototype.pointerUp = function (clientX, clientY, rect) {
    var press = this._press;
    this._press = null;
    this.indicator = null;
    if (!press || !this.ready()) return null;
    var kind = classifyGesture(press.display, { x: clientX, y: clientY });
    var payload;
    if (kind === "tap") {
      payload = { action: "tap", x: press.device.x, y: press.device.y };
    } else {
      var end = mapDisplayToDevice(clientX, clientY, rect, this.deviceWidth, this.deviceHeight);
      if (!end) return null;
      payload = { action: "swipe", x: press.device.x, y: press.device.y, x2: end.x, y2: end.y };
    }
    if (!ALLOWED[payload.action] || FORBIDDEN[payload.action]) return null;
    this._send(payload);
    return payload;
  };

  PhoneRemoteController.prototype.pointerCancel = function () {
    this._press = null;
    this.indicator = null;
  };

  PhoneRemoteController.prototype.sendBack = function () {
    if (!this.ready()) return null;
    var payload = { action: "back" };
    this._send(payload);
    return payload;
  };

  PhoneRemoteController.prototype.sendType = function (text) {
    if (!this.ready()) return null;
    var value = String(text || "").slice(0, 64);
    if (!value) return null;
    var payload = { action: "type", text: value };
    this._send(payload);
    return payload;
  };

  function attach(root, options) {
    options = options || {};
    var send = options.send;
    var controller = new PhoneRemoteController(send);
    var canvas = root.querySelector("canvas");
    var hit = root.querySelector(".phone-remote-hit");
    var dot = root.querySelector(".phone-remote-dot");
    if (!canvas || !hit) throw new Error("phone-remote-surface markup missing canvas/hit layer");

    function rect() {
      return hit.getBoundingClientRect();
    }

    function paintBlob(blob) {
      return createImageBitmap(blob).then(function (bmp) {
        if (canvas.width !== bmp.width) canvas.width = bmp.width;
        if (canvas.height !== bmp.height) canvas.height = bmp.height;
        canvas.getContext("2d").drawImage(bmp, 0, 0);
        controller.onFrame(bmp.width, bmp.height);
        bmp.close();
        hit.dataset.ready = controller.ready() ? "true" : "false";
      });
    }

    function syncDot() {
      if (!dot) return;
      if (!controller.indicator) {
        dot.hidden = true;
        return;
      }
      dot.hidden = false;
      dot.style.left = controller.indicator.x + "px";
      dot.style.top = controller.indicator.y + "px";
    }

    function onDown(ev) {
      ev.preventDefault();
      ev.stopPropagation();
      if (hit.setPointerCapture) {
        try {
          hit.setPointerCapture(ev.pointerId);
        } catch (ignore) {}
      }
      controller.setSessionConnected(options.sessionConnected !== false);
      controller.pointerDown(ev.clientX, ev.clientY, rect());
      syncDot();
    }

    function onMove(ev) {
      if (!controller._press) return;
      ev.preventDefault();
      ev.stopPropagation();
      controller.pointerMove(ev.clientX, ev.clientY, rect());
      syncDot();
    }

    function onUp(ev) {
      ev.preventDefault();
      ev.stopPropagation();
      controller.pointerUp(ev.clientX, ev.clientY, rect());
      syncDot();
    }

    hit.addEventListener("pointerdown", onDown);
    hit.addEventListener("pointermove", onMove);
    hit.addEventListener("pointerup", onUp);
    hit.addEventListener("pointercancel", function () {
      controller.pointerCancel();
      syncDot();
    });
    hit.addEventListener("contextmenu", function (ev) {
      ev.preventDefault();
    });
    hit.addEventListener("dragstart", function (ev) {
      ev.preventDefault();
    });
    hit.addEventListener(
      "touchmove",
      function (ev) {
        ev.preventDefault();
      },
      { passive: false }
    );

    return {
      controller: controller,
      paintBlob: paintBlob,
      mapDisplayToDevice: mapDisplayToDevice,
      classifyGesture: classifyGesture,
    };
  }

  return {
    ALLOWED: ALLOWED,
    FORBIDDEN: FORBIDDEN,
    SWIPE_THRESHOLD_PX: SWIPE_THRESHOLD_PX,
    mapDisplayToDevice: mapDisplayToDevice,
    classifyGesture: classifyGesture,
    PhoneRemoteController: PhoneRemoteController,
    attach: attach,
  };
});
