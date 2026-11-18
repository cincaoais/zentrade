(function () {
  "use strict";

  const ZT = (window.ZT = window.ZT || {});

  const GREEN = "#22c55e";
  const RED = "#ef4444";

  // ---------------------------------------------------------------------------
  // Pure helpers (no THREE / DOM): safe to run under node.
  // ---------------------------------------------------------------------------

  function indexAtOrBefore(days, iso) {
    let lo = 0;
    let hi = days.length - 1;
    let ans = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (days[mid] <= iso) {
        ans = mid;
        lo = mid + 1;
      } else {
        hi = mid - 1;
      }
    }
    return ans;
  }

  function closeAtOrBefore(bars, symbol, iso) {
    const b = bars && bars[symbol];
    if (!b || !Array.isArray(b.d) || !Array.isArray(b.c)) return null;
    const idx = indexAtOrBefore(b.d, iso);
    if (idx < 0) return null;
    const c = b.c[idx];
    return c == null || !isFinite(c) ? null : c;
  }

  // Point-in-time boxes for replay day i. A trade is open when it has not
  // exited by days[i]; its height uses the close at days[i], never exit_price.
  function _boxes(data, i) {
    const days = (data && data.days) || [];
    if (!data || typeof i !== "number" || !isFinite(i) || i < 0 || i >= days.length) return [];
    const day = days[i];
    const trades = data.trades || [];
    const out = [];

    for (let k = 0; k < trades.length; k++) {
      const t = trades[k];
      if (!t || !t.entry_date || t.entry_date > day) continue;
      const x0 = indexAtOrBefore(days, t.entry_date);
      if (x0 < 0) continue;

      const isOpen = !t.exit_date || t.exit_date > day;
      let x1;
      let ret;

      if (isOpen) {
        x1 = i;
        const close = closeAtOrBefore(data.bars, t.symbol, day);
        ret = close != null && t.entry_price ? close / t.entry_price - 1 : 0;
      } else {
        x1 = indexAtOrBefore(days, t.exit_date);
        if (x1 < x0) x1 = x0;
        if (x1 > i) x1 = i;
        ret = t.exit_price != null && t.entry_price ? t.exit_price / t.entry_price - 1 : 0;
      }

      out.push({
        id: t.id,
        x0: x0,
        x1: x1,
        z: t.slot,
        h: 0.2 + 4 * Math.abs(ret),
        color: ret >= 0 ? GREEN : RED,
        open: isOpen,
      });
    }

    return out;
  }

  // ---------------------------------------------------------------------------
  // three.js view.
  // ---------------------------------------------------------------------------

  function slotCount(data) {
    const metaSlots = data && data.meta && data.meta.slots;
    if (metaSlots) return Math.max(1, Math.round(metaSlots));
    let maxSlot = -1;
    const trades = (data && data.trades) || [];
    for (let k = 0; k < trades.length; k++) {
      const s = trades[k] && trades[k].slot;
      if (typeof s === "number" && s > maxSlot) maxSlot = s;
    }
    return Math.max(1, maxSlot + 1);
  }

  function create(container, data) {
    const THREE = window.THREE;
    const OrbitControls =
      (window.ZT && window.ZT.OrbitControls) || window.OrbitControls;

    if (!container) throw new Error("ZT.timeline.create: container is required");
    if (!THREE) throw new Error("ZT.timeline.create: window.THREE is not available");
    if (!OrbitControls) {
      throw new Error("ZT.timeline.create: ZT.OrbitControls is not available");
    }

    data = data || {};
    const days = Array.isArray(data.days) ? data.days : [];
    const trades = Array.isArray(data.trades) ? data.trades : [];
    const slots = slotCount(data);
    const laneCenter = (slots - 1) / 2;
    const wallZ = -1.5;
    const wallHeight = 6;
    const wallBase = 0.2;

    let current = -1;
    let windowDays = 126;
    const TARGET_WIDTH = 2.2 * slots;
    let dx = TARGET_WIDTH / Math.max(1, windowDays);
    let selectedId = null;
    let selectCb = null;
    let needsRender = true;
    let running = true;
    let rafId = null;
    let lastCenter = null;

    function measure() {
      const rect =
        typeof container.getBoundingClientRect === "function"
          ? container.getBoundingClientRect()
          : null;
      const w = Math.max(
        1,
        Math.round((rect && rect.width) || container.clientWidth || 800)
      );
      const h = Math.max(
        1,
        Math.round((rect && rect.height) || container.clientHeight || 600)
      );
      return { w: w, h: h };
    }

    function updateDx() {
      dx = TARGET_WIDTH / Math.max(1, windowDays);
    }

    const dims = measure();
    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.setSize(dims.w, dims.h);
    renderer.setClearColor(new THREE.Color("#0b0f17"), 1);
    renderer.domElement.style.display = "block";
    container.appendChild(renderer.domElement);

    const scene = new THREE.Scene();
    scene.background = new THREE.Color("#0b0f17");

    const camera = new THREE.PerspectiveCamera(45, dims.w / dims.h, 0.1, 5000);
    camera.position.set(0, 8, 20);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.addEventListener("change", function () {
      needsRender = true;
    });

    scene.add(new THREE.AmbientLight(0xffffff, 0.75));
    const key = new THREE.DirectionalLight(0xffffff, 1.1);
    key.position.set(6, 14, 10);
    scene.add(key);
    const fill = new THREE.DirectionalLight(0x93c5fd, 0.35);
    fill.position.set(-8, 6, -10);
    scene.add(fill);

    // --- lane strips -------------------------------------------------------
    const laneCount = Math.max(days.length, 1);
    const laneGeo = new THREE.PlaneGeometry(1, 1);
    const laneMat = new THREE.MeshBasicMaterial({
      color: 0x121a28,
      transparent: true,
      opacity: 0.8,
      side: THREE.DoubleSide,
      depthWrite: false,
    });
    const laneStrips = [];
    for (let s = 0; s < slots; s++) {
      const strip = new THREE.Mesh(laneGeo, laneMat);
      strip.rotation.x = -Math.PI / 2;
      strip.scale.set(laneCount * dx, 0.9, 1);
      strip.position.set(((laneCount - 1) / 2) * dx, -0.02, s);
      scene.add(strip);
      laneStrips.push(strip);
    }

    // --- text sprites ------------------------------------------------------
    function textSprite(text, opts) {
      opts = opts || {};
      const fontSize = opts.fontSize || 46;
      const pad = 14;
      const canvas = document.createElement("canvas");
      const ctx = canvas.getContext("2d");
      const font =
        "600 " + fontSize + "px system-ui, -apple-system, 'Segoe UI', sans-serif";
      ctx.font = font;
      const measured = ctx.measureText(text).width;
      canvas.width = Math.max(2, Math.ceil(measured + pad * 2));
      canvas.height = fontSize + pad * 2;
      ctx.font = font;
      ctx.fillStyle = opts.color || "#9ca3af";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(text, canvas.width / 2, canvas.height / 2);

      const texture = new THREE.CanvasTexture(canvas);
      if (THREE.SRGBColorSpace) texture.colorSpace = THREE.SRGBColorSpace;
      texture.minFilter = THREE.LinearFilter;
      const material = new THREE.SpriteMaterial({
        map: texture,
        transparent: true,
        depthWrite: false,
        depthTest: false,
      });
      const sprite = new THREE.Sprite(material);
      const worldHeight = opts.height || 1.2;
      const aspect = canvas.width / canvas.height;
      sprite.scale.set(worldHeight * aspect, worldHeight, 1);
      sprite.renderOrder = 10;
      return sprite;
    }

    const laneLabels = [];
    for (let s = 0; s < slots; s++) {
      const sprite = textSprite("slot " + (s + 1), {
        color: "#cbd5e1",
        height: 1.15,
      });
      sprite.position.set(-2.5, 0.45, s);
      scene.add(sprite);
      laneLabels.push({ sprite: sprite, slot: s });
    }

    // Month ticks: a label on the first trading day of each month.
    const MONTHS = [
      "Jan", "Feb", "Mar", "Apr", "May", "Jun",
      "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ];
    const monthSprites = [];
    for (let k = 0; k < days.length; k++) {
      const iso = days[k];
      const month = Number(iso.slice(5, 7)) - 1;
      const prevMonth = k > 0 ? Number(days[k - 1].slice(5, 7)) - 1 : -1;
      if (k > 0 && month === prevMonth && iso.slice(0, 4) === days[k - 1].slice(0, 4)) {
        continue;
      }
      const yy = iso.slice(2, 4);
      const text = MONTHS[month] + " '" + yy;
      const sprite = textSprite(text, { color: "#64748b", height: 0.9, fontSize: 40 });
      sprite.position.set(k * dx, 0.35, slots + 0.9);
      scene.add(sprite);
      monthSprites.push({ sprite: sprite, day: k });
    }

    // --- back wall with equity / SPY --------------------------------------
    const wallGeo = new THREE.PlaneGeometry(1, 1);
    const wallMat = new THREE.MeshBasicMaterial({
      color: 0x0d1420,
      side: THREE.DoubleSide,
    });
    const wall = new THREE.Mesh(wallGeo, wallMat);
    wall.scale.set(laneCount * dx, wallHeight, 1);
    wall.position.set(((laneCount - 1) / 2) * dx, wallBase + wallHeight / 2, wallZ - 0.05);
    scene.add(wall);

    // Fixed y scale over the whole period so the lines never rescale mid-replay.
    const scaleValues = [];
    const equityValues = Array.isArray(data.equity) ? data.equity : [];
    const spyValues = Array.isArray(data.spy) ? data.spy : [];
    for (let k = 0; k < equityValues.length; k++) {
      if (equityValues[k] != null && isFinite(equityValues[k])) scaleValues.push(equityValues[k]);
    }
    for (let k = 0; k < spyValues.length; k++) {
      if (spyValues[k] != null && isFinite(spyValues[k])) scaleValues.push(spyValues[k]);
    }
    scaleValues.push(1);
    let wallLo = Math.min.apply(null, scaleValues);
    let wallHi = Math.max.apply(null, scaleValues);
    if (!(wallHi > wallLo)) wallHi = wallLo + 1;
    const wallSpan = wallHi - wallLo;

    function wallY(v) {
      return wallBase + ((v - wallLo) / wallSpan) * wallHeight;
    }

    function writeLinePositions(line) {
      const values = line.userData.values;
      const fallback = line.userData.fallback;
      const attr = line.geometry.getAttribute("position");
      const positions = attr.array;
      let last = fallback == null ? 1 : fallback;
      for (let k = 0; k < days.length; k++) {
        let v = values[k];
        if (v == null || !isFinite(v)) v = last;
        else last = v;
        positions[k * 3] = k * dx;
        positions[k * 3 + 1] = wallY(v);
        positions[k * 3 + 2] = wallZ;
      }
      attr.needsUpdate = true;
      line.geometry.computeBoundingSphere();
    }

    function makeLine(values, color, fallback) {
      const positions = new Float32Array(Math.max(days.length, 1) * 3);
      const geo = new THREE.BufferGeometry();
      geo.setAttribute("position", new THREE.BufferAttribute(positions, 3));
      geo.setDrawRange(0, 0);
      const mat = new THREE.LineBasicMaterial({
        color: color,
        transparent: true,
        opacity: 0.9,
      });
      const line = new THREE.Line(geo, mat);
      line.userData.values = values;
      line.userData.fallback = fallback;
      writeLinePositions(line);
      scene.add(line);
      return line;
    }

    const equityLine = makeLine(equityValues, 0xffffff, 1);
    const spyLine = makeLine(spyValues, 0x9ca3af, 1);

    function refreshLines() {
      writeLinePositions(equityLine);
      writeLinePositions(spyLine);
    }

    // --- cursor ------------------------------------------------------------
    const cursorGeo = new THREE.PlaneGeometry(1, 1);
    const cursorMat = new THREE.MeshBasicMaterial({
      color: 0x60a5fa,
      transparent: true,
      opacity: 0.16,
      side: THREE.DoubleSide,
      depthWrite: false,
    });
    const cursor = new THREE.Mesh(cursorGeo, cursorMat);
    cursor.rotation.y = Math.PI / 2;
    cursor.scale.set(1, wallHeight + 0.4, 1);
    cursor.position.set(0, wallBase + wallHeight / 2, laneCenter);
    cursor.visible = false;
    scene.add(cursor);

    // --- trade boxes -------------------------------------------------------
    const boxGeo = new THREE.BoxGeometry(1, 1, 1);
    const closedGreen = new THREE.MeshStandardMaterial({
      color: new THREE.Color(GREEN),
      roughness: 0.5,
      metalness: 0.12,
    });
    const closedRed = new THREE.MeshStandardMaterial({
      color: new THREE.Color(RED),
      roughness: 0.5,
      metalness: 0.12,
    });
    const openGreen = new THREE.MeshStandardMaterial({
      color: new THREE.Color(GREEN),
      roughness: 0.5,
      metalness: 0.12,
      transparent: true,
      opacity: 0.45,
      depthWrite: false,
    });
    const openRed = new THREE.MeshStandardMaterial({
      color: new THREE.Color(RED),
      roughness: 0.5,
      metalness: 0.12,
      transparent: true,
      opacity: 0.45,
      depthWrite: false,
    });

    const tradeMeshes = [];
    const meshById = new Map();
    const tradeById = new Map();
    for (let k = 0; k < trades.length; k++) {
      const t = trades[k];
      if (!t) continue;
      const mesh = new THREE.Mesh(boxGeo, closedGreen);
      mesh.visible = false;
      mesh.userData.tradeId = t.id;
      mesh.position.set(0, 0, typeof t.slot === "number" ? t.slot : 0);
      scene.add(mesh);
      tradeMeshes.push(mesh);
      meshById.set(t.id, mesh);
      tradeById.set(t.id, t);
    }

    const edgesGeo = new THREE.EdgesGeometry(boxGeo);
    const outlineMat = new THREE.LineBasicMaterial({
      color: 0xfbbf24,
      transparent: true,
      opacity: 0.95,
      depthTest: false,
    });
    const outline = new THREE.LineSegments(edgesGeo, outlineMat);
    outline.visible = false;
    outline.renderOrder = 6;
    scene.add(outline);

    // --- camera framing ----------------------------------------------------
    function fitDistance() {
      const vfov = (camera.fov * Math.PI) / 180;
      const aspect = Math.max(0.2, camera.aspect || 1);
      const hfov = 2 * Math.atan(Math.tan(vfov / 2) * aspect);
      const visibleWidth = Math.max(4, windowDays * dx);
      const widthDist = (visibleWidth / 2 / Math.tan(hfov / 2)) * 1.2;
      const heightDist =
        (Math.max(6, slots + 2) / 2 / Math.tan(vfov / 2)) * 1.3;
      return Math.max(12, widthDist, heightDist);
    }

    function follow(i, force) {
      // Frame the window that ends at day i; before day windowDays-1 keep the
      // window anchored at day 0 so the first trades are not pushed off-screen.
      const span = Math.max(1, windowDays) - 1;
      const left = Math.max(0, (i == null ? 0 : i) - span);
      const center = (left + span / 2) * dx;
      if (force || lastCenter === null) {
        controls.target.set(center, wallBase + wallHeight * 0.3, laneCenter);
        const dir = new THREE.Vector3(0.18, 0.62, 0.76).normalize();
        camera.position
          .copy(controls.target)
          .addScaledVector(dir, fitDistance());
        controls.update();
      } else {
        const dx = center - lastCenter;
        camera.position.x += dx;
        controls.target.x += dx;
        controls.update();
      }
      lastCenter = center;
    }

    function updateLaneLabels() {
      const left = Math.max(0, current - Math.max(1, windowDays) + 1);
      for (let k = 0; k < laneLabels.length; k++) {
        laneLabels[k].sprite.position.x = (left - 2.5) * dx;
      }
    }

    function updateMonthTicks() {
      for (let k = 0; k < monthSprites.length; k++) {
        monthSprites[k].sprite.position.x = monthSprites[k].day * dx;
      }
    }

    // Re-apply the current day width to everything that spans the x axis.
    function applyDayScale() {
      const axisWidth = Math.max(days.length, 1) * dx;
      const axisCenter = ((Math.max(days.length, 1) - 1) / 2) * dx;
      for (let k = 0; k < laneStrips.length; k++) {
        laneStrips[k].scale.x = axisWidth;
        laneStrips[k].position.x = axisCenter;
      }
      wall.scale.x = axisWidth;
      wall.position.x = axisCenter;
      updateMonthTicks();
      updateLaneLabels();
    }

    function updateOutline() {
      const mesh = selectedId == null ? null : meshById.get(selectedId);
      if (!mesh || !mesh.visible) {
        outline.visible = false;
        return;
      }
      outline.visible = true;
      outline.position.copy(mesh.position);
      outline.rotation.copy(mesh.rotation);
      outline.scale.copy(mesh.scale).multiplyScalar(1.03);
    }

    function setDay(i) {
      current = Math.max(-1, Math.min(typeof i === "number" ? i : -1, days.length - 1));
      const boxes = _boxes(data, current);
      for (let k = 0; k < tradeMeshes.length; k++) tradeMeshes[k].visible = false;

      for (let k = 0; k < boxes.length; k++) {
        const b = boxes[k];
        const mesh = meshById.get(b.id);
        if (!mesh) continue;
        const width = Math.max(0.2, b.x1 - b.x0);
        const cx = Math.min((b.x0 + b.x1) / 2, current - width / 2);
        mesh.visible = true;
        mesh.position.set(cx * dx, b.h / 2, b.z);
        mesh.scale.set(width * dx, b.h, 0.72);
        if (b.open) {
          mesh.material = b.color === GREEN ? openGreen : openRed;
        } else {
          mesh.material = b.color === GREEN ? closedGreen : closedRed;
        }
      }

      updateOutline();

      cursor.visible = current >= 0;
      cursor.position.x = current * dx;

      const count = Math.max(0, current + 1);
      equityLine.geometry.setDrawRange(0, count);
      spyLine.geometry.setDrawRange(0, count);

      updateLaneLabels();
      follow(current, false);
      needsRender = true;
    }

    function setWindow(n) {
      const next = Math.round(Number(n));
      windowDays = isFinite(next) && next > 0 ? next : 126;
      updateDx();
      refreshLines();
      applyDayScale();
      lastCenter = null;
      setDay(current);
      follow(current, true);
      needsRender = true;
    }

    function select(id) {
      selectedId = id == null ? null : id;
      updateOutline();
      needsRender = true;
    }

    function onSelect(cb) {
      selectCb = typeof cb === "function" ? cb : null;
    }

    function resize() {
      const d = measure();
      renderer.setSize(d.w, d.h);
      camera.aspect = d.w / d.h;
      camera.updateProjectionMatrix();
      lastCenter = null;
      follow(current, true);
      needsRender = true;
    }

    function dispose() {
      running = false;
      if (rafId != null && window.cancelAnimationFrame) {
        window.cancelAnimationFrame(rafId);
      }
      rafId = null;

      renderer.domElement.removeEventListener("pointerdown", onPointerDown);
      renderer.domElement.removeEventListener("pointerup", onPointerUp);
      controls.dispose();

      const seen = new Set();
      scene.traverse(function (obj) {
        if (obj.geometry && !seen.has(obj.geometry)) {
          seen.add(obj.geometry);
          obj.geometry.dispose();
        }
        const mats = Array.isArray(obj.material)
          ? obj.material
          : obj.material
          ? [obj.material]
          : [];
        for (let k = 0; k < mats.length; k++) {
          if (seen.has(mats[k])) continue;
          seen.add(mats[k]);
          if (mats[k].map) mats[k].map.dispose();
          mats[k].dispose();
        }
      });

      renderer.dispose();
      if (renderer.domElement.parentNode) {
        renderer.domElement.parentNode.removeChild(renderer.domElement);
      }
      if (typeof scene.clear === "function") scene.clear();
    }

    // --- picking -----------------------------------------------------------
    const raycaster = new THREE.Raycaster();
    const pointer = new THREE.Vector2();
    let downPoint = null;

    function onPointerDown(event) {
      downPoint = { x: event.clientX, y: event.clientY };
    }

    function onPointerUp(event) {
      if (!downPoint) return;
      const dx = event.clientX - downPoint.x;
      const dy = event.clientY - downPoint.y;
      downPoint = null;
      if (Math.sqrt(dx * dx + dy * dy) >= 5) return;

      const rect = renderer.domElement.getBoundingClientRect();
      pointer.x = ((event.clientX - rect.left) / Math.max(1, rect.width)) * 2 - 1;
      pointer.y = -((event.clientY - rect.top) / Math.max(1, rect.height)) * 2 + 1;
      raycaster.setFromCamera(pointer, camera);

      const pickable = [];
      for (let k = 0; k < tradeMeshes.length; k++) {
        if (tradeMeshes[k].visible) pickable.push(tradeMeshes[k]);
      }
      const hits = raycaster.intersectObjects(pickable, false);
      if (!hits.length) return;
      const id = hits[0].object.userData.tradeId;
      select(id);
      if (selectCb) selectCb(tradeById.get(id));
    }

    renderer.domElement.addEventListener("pointerdown", onPointerDown);
    renderer.domElement.addEventListener("pointerup", onPointerUp);

    // --- render loop: only draw when something changed / controls move -----
    function animate() {
      if (!running) return;
      rafId = window.requestAnimationFrame(animate);
      if (controls.update()) needsRender = true;
      if (needsRender) {
        needsRender = false;
        renderer.render(scene, camera);
      }
    }

    setDay(0);
    animate();

    return {
      setDay: setDay,
      setWindow: setWindow,
      select: select,
      onSelect: onSelect,
      resize: resize,
      dispose: dispose,
    };
  }

  ZT.timeline = {
    create: create,
    _boxes: _boxes,
  };
})();