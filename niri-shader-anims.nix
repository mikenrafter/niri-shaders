# Animation tuple registry for niri custom shaders.
#
# Structure:
#   animations  — canonical GLSL body strings keyed by semantic name
#   simple/full — shader profiles composing animation tuples from the registry
#
# Each profile entry exports:
#   close, open, resize — lists of animation tuples:
#     { fn, body, durationMs, prob?, needsVoronoiBake?, includeWhen?, forceWhen? }
#   closeSharedPreamble, openSharedPreamble, resizeSharedPreamble
#
# Optional routing predicates (GLSL bool expressions, injected verbatim):
#   includeWhen — filters the seed-routing pool only; absent = always eligible
#   forceWhen     — scanned on the full list in index order; first match wins,
#                   bypassing includeWhen and seed bands
#
# durationMs is required on every animation tuple. The compositor runs for
# max(durationMs) per event; the seed router scales shorter animations to
# finish at durationMs / max(durationMs) of global progress.
#
# Profile composition: full extends simple via list append (simple.close ++ [ … ]).
# Probabilities: explicit prob values must sum to ≤ 1.0; unspecified entries share
# the remainder equally (see fillDefaultProbs in niri-shader-lib.nix).
#
# Tuple `fn` names match GLSL function symbols; mkSeedRouter generates close_color /
# open_color / resize_color entry points that dispatch to them.

let
  # ── Per-animation wall-clock durations (ms) ───────────────────────────────
  DUR_CLOSE_UNHOOK = 900;
  DUR_CLOSE_SHREDDER = 3000;
  DUR_CLOSE_VORONOI = 1200;
  DUR_OPEN_NGON = 1000;
  DUR_OPEN_VORONOI = 1000;
  DUR_RESIZE_CRT = 1000;

  # Shredder physics constants were tuned for a 1200 ms timeline; scale t so
  # pieces fall off-screen when the compositor runs at DUR_CLOSE_SHREDDER.
  # Runtime fall extent also scales via niri_output_size / niri_window_pos so
  # pieces clear the visible output, not just the window bottom.
  SHREDDER_PHYSICS_REF_MS = 1200;
  closeTotalMs = DUR_CLOSE_SHREDDER;
  SHREDDER_PHYSICS_SCALE = closeTotalMs / (SHREDDER_PHYSICS_REF_MS * 1.0);

  # ── Animation B/V phase boundaries (fraction of 1200 ms) ─────────────────
  T1 = 0.0833;
  T2 = 0.125;
  T3 = 0.2917;
  VW = 0.45;

  invT1 = 1.0 / T1;
  invT2T1 = 1.0 / (T2 - T1);
  invT3T2 = 1.0 / (T3 - T2);
  halfInvVW = 0.5 / VW;
  inv1200 = 1.0 / 1200.0;

  # ── Animation C: Shredder constants ──────────────────────────────────────
  STRIP_W_C = 75.0;
  SEG_H_C = 150.0;
  GRAVITY_C = 2400.0;
  SLIDE_END_C = 0.4;
  DRIFT_MAX_C = 900.0;
  V0_UP_C = 500.0;
  WHITE_MIX_C = 0.2;
  ABERR_SCALE_C = 0.004;
  SCATTER_STRIPS_C = 16;

  INV_SLIDE_END_C = 1.0 / SLIDE_END_C;
  GRAVITY_HALF_C = 0.5 * GRAVITY_C;
  INV_STRIP_W_C = 1.0 / STRIP_W_C;
  INV_COS_CLAMP_C = 1.0 / 0.05;
  FLUTTER_AMP_C = 0.18;
  FLUTTER_FREQ_C = 15.0;
  TUMBLE_RATE_BASE_C = 8.0;
  TUMBLE_RATE_SCALE_C = 16.0;
  TWO_PI_C = 6.2831853;
  MAX_HALF_H_C = 0.5 * SEG_H_C + 0.5 * STRIP_W_C * (FLUTTER_AMP_C / 0.05) + 2.0;
  MAX_HALF_W_C = 0.5 * STRIP_W_C * INV_COS_CLAMP_C + 0.5 * SEG_H_C + 2.0;

  debugC = false;

  # ── Animation Open A/B/V constants ───────────────────────────────────────
  OPEN_A_SETTLE_START = 0.88;
  OPEN_B_T_CRACK_END = 0.35;
  OPEN_B_SETTLE_START = 0.88;
  invOpenBCrackEnd = 1.0 / OPEN_B_T_CRACK_END;
  invOpenBSettleSpan = 1.0 / (1.0 - OPEN_B_SETTLE_START);
  inv1000 = 1.0 / 1000.0;
  OPEN_B_CRUMBLE_SPAN = 0.55 * (1.0 - OPEN_B_T_CRACK_END);
  OPEN_V_CRACK_OP = 0.7;
  OPEN_V_FLYING_ALPHA = 0.5;
  OPEN_V_FLYING_RGB = 136.0 / 255.0;
  CLOSE_V_GLASS_ALPHA = 0.30;   # 60% of OPEN_V_FLYING_ALPHA — subtle frost on crumble shards
  CLOSE_V_GLASS_RGB = 136.0 / 255.0;

  debugOpenV = false;
  debugOpenVMode = 3;

  # ── Animation R: CRT Rip (window-resize, 1000 ms) ────────────────────────
  RAMP_IN_R = 0.10;
  RAMP_OUT_R = 0.80;
  SCANLINE_DEPTH_R = 0.25;
  FLICKER_AMP_R = 0.06;
  FLICKER_FREQ_R = 18.0;
  RGB_SPLIT_R = 2.0;
  BARREL_K_R = 0.12;
  TEAR_BASE_HMIN_F_R = 0.0088;
  TEAR_BASE_HMAX_F_R = 0.0242;
  TEAR_MARGIN_R = 0.04;
  TEAR_CX_MARGIN_R = 0.10;
  TEAR_HW_MIN_R = 0.080;
  TEAR_HW_MAX_R = 0.140;
  TEAR_ONSET_MIN_R = 0.10;
  TEAR_ONSET_JITTER_R = 0.35;
  TEAR_SPAN_MIN_R = 0.18;
  TEAR_SPAN_JITTER_R = 0.12;
  RAIN_COL_W_R = 9.0;
  RAIN_GLYPH_H_R = 16.0;
  RAIN_SCROLL_SPD_R = 1200.0;
  RAIN_TRAIL_LEN_R = 150.0;
  INV_SCANLINE_R = 1.0 / 4.0;
  INV_RAIN_COL_W_R = 1.0 / 9.0;
  INV_RAIN_GLYPH_H_R = 1.0 / 16.0;
  INV_RAIN_TRAIL_R = 1.0 / 150.0;
  GLITCH_BAND_H_R = 8.0;
  GLITCH_ACT_PROB_R = 0.18;
  GLITCH_MAX_SHIFT_R = 0.04;
  GLITCH_TICK_RATE_R = 12.0;
  GLITCH_ABERR_MUL_R = 0.25;
  INV_GLITCH_BAND_H_R = 0.125;

  # ── Shared preamble: fast_sincos + wrap_pm_pi (needed by close anims A, C, V) ──
  fastSincosPreamble = ''
    // ── Polynomial sin/cos (Bhaskara-inspired domain fold + Horner Taylor) ──
    //
    // WHY: On iGPUs (e.g. Intel Xe) hardware sin/cos go through extended-math
    // units at ~20-30 clocks each.  A polynomial approximation uses only FMA
    // instructions at ~1 clock each, so ~22 ALU ops beats ~24-60 EMU clocks.
    //
    // HOW — three steps:
    //   1. Domain fold to [0, π/2].
    //      The identity sin(π - x) = sin(x) lets us reflect any x ∈ [0, π]
    //      down to [0, π/2] via  fx = min(|x|, π - |x|).
    //      Taylor converges tightly on [0, π/2], so 3 terms give < 2×10⁻⁵ error.
    //
    //   2. Horner evaluation (minimises multiply count):
    //        sin(fx) ≈ fx × (1 − fx²×(1/6 − fx²×(1/120 − fx²/5040)))
    //        cos(fx) ≈ 1  − fx²×(1/2 − fx²×(1/24  − fx²/720))
    //
    //   3. Restore signs:
    //        sin is an odd function  →  multiply result by sign(x).
    //        cos(π - x) = -cos(x)   →  negate when |x| > π/2 (branchless step).
    //
    // Max error after folding: ~1.9×10⁻⁵ (sin), ~1.4×10⁻⁵ (cos).
    // Sharing abs/fold/fxsq between sin and cos saves ~4 ops vs calling twice.
    void fast_sincos(float x, out float sv, out float cv) {
        const float PI      = 3.14159265;
        const float HALF_PI = 1.5707963;
        float ax   = abs(x);
        float fx   = min(ax, PI - ax);    // fold |x| ∈ [0,π] → fx ∈ [0,π/2]
        float fxsq = fx * fx;
        // sin: odd function — restore original sign after polynomial
        sv = sign(x) * fx
             * (1.0 - fxsq * (0.16666667 - fxsq * (0.00833333 - fxsq * 0.00019841)));
        // cos: negate when reflected (|x| > π/2), since cos(π-x) = -cos(x)
        float cosraw = 1.0 - fxsq * (0.5 - fxsq * (0.04166667 - fxsq * 0.00138889));
        cv = cosraw * (1.0 - 2.0 * step(HALF_PI, ax));
    }

    // Reduce arbitrary angles to [-π, π) so fast_sincos stays in its intended domain.
    float wrap_pm_pi(float x) {
        const float PI  = 3.14159265;
        const float TAU = 6.2831853;
        return x - TAU * floor((x + PI) / TAU);
    }
  '';

  # ── Shared preamble: voronoi runtime helpers (close V, open V) ───────────
  voronoiRuntimePreamble = ''
    float voronoi_gap_norm(int variant, vec2 q_norm) {
        float sq1 = 1e18;
        float sq2 = 1e18;
        for (int i = 0; i < 16; i++) {
            int idx = baked_cell_index(variant, i) * 2;
            vec2  c = vec2(BAKED_CC_NXY_raw(idx), BAKED_CC_NXY_raw(idx + 1));
            vec2  d = q_norm - c;
            float dsq = dot(d, d);
            if (dsq < sq1) {
                sq2 = sq1;
                sq1 = dsq;
            } else if (dsq < sq2) {
                sq2 = dsq;
            }
        }
        return sq2 - sq1;
    }

    float voronoi_edge_norm(int variant, vec2 q_norm) {
        float sq1 = 1e18;
        float sq2 = 1e18;
        vec2  c1  = vec2(0.0);
        vec2  c2  = vec2(0.0);
        for (int i = 0; i < 16; i++) {
            int idx = baked_cell_index(variant, i) * 2;
            vec2  c = vec2(BAKED_CC_NXY_raw(idx), BAKED_CC_NXY_raw(idx + 1));
            vec2  d = q_norm - c;
            float dsq = dot(d, d);
            if (dsq < sq1) {
                sq2 = sq1; c2 = c1;
                sq1 = dsq; c1 = c;
            } else if (dsq < sq2) {
                sq2 = dsq; c2 = c;
            }
        }
        vec2  c12  = c1 - c2;
        float denom = max(length(c12), 1e-6);
        return 0.5 * (sq2 - sq1) / denom;
    }

    int baked_owner_cell_runtime(int variant, vec2 q_norm) {
        float sq1 = 1e18;
        int   owner = 0;
        for (int i = 0; i < 16; i++) {
            int idx = baked_cell_index(variant, i) * 2;
            vec2  c = vec2(BAKED_CC_NXY_raw(idx), BAKED_CC_NXY_raw(idx + 1));
            vec2  d = q_norm - c;
            float dsq = dot(d, d);
            if (dsq < sq1) {
                sq1 = dsq;
                owner = i;
            }
        }
        return owner;
    }
  '';

  animations = {
    close = {
      unhook-fall = ''
        // ── Unhook & Fall ────────────────────────────────────────
        //
        // Simple arc trajectory — no voronoi bake needed.
        // The window is "unhooked" from the top and falls with a random lateral arc.
        vec4 close_unhook_fall(vec3 cg, vec3 sg, float t, float s) {
            float ang  = s * 3.14159265 + 3.14159265;
            vec2  v0   = vec2(cos(ang), sin(ang));
            vec2  off  = vec2(v0.x * 240.0 * t,
                              v0.y * 240.0 * t + 350.0 * t * t);
            float apex = clamp(-v0.y * 240.0 / 700.0, 0.0, 1.0);
            float a    = 1.0 - clamp((t - apex) / max(1.0 - apex, 0.001), 0.0, 1.0);
            vec3  cg2  = cg;
            cg2.xy    -= off / sg.xy;
            vec3  ct   = niri_geo_to_tex * cg2;
            float ib   = step(0.0, ct.x) * step(ct.x, 1.0)
                       * step(0.0, ct.y) * step(ct.y, 1.0);
            return texture(niri_tex, clamp(ct.st, vec2(0.0), vec2(1.0))) * (a * ib);
        }
      '';

      shredder = ''
        // ── Shredder ─────────────────────────────────────────────
        //
        // The window slides upward into an implied shredder at the top.  Each
        // (strip × segment) cell is "cut" when its top row reaches y=0, then
        // falls under gravity with horizontal drift, Y-axis tumble (3-D
        // foreshortening), and Z-axis flutter.
        //
        // No voronoi bake needed.
        // Constants injected at build time from the Nix let-block above.
        vec4 close_shredder(vec3 cg, vec3 sg, float t_prog, float ls) {
            vec2  sz         = sg.xy;
            vec2  px         = cg.xy * sz;
            vec2  inv_sz     = vec2(1.0 / sz.x, 1.0 / sz.y);
            float STRIP_W    = ${toString STRIP_W_C};
            float SEG_H      = ${toString SEG_H_C};
            float GRAVITY_HALF = ${toString GRAVITY_HALF_C};
            float DRIFT_MAX  = ${toString DRIFT_MAX_C};
            float V0_UP      = ${toString V0_UP_C};
            float WHITE_MIX  = ${toString WHITE_MIX_C};
            float ABERR_SCALE = ${toString ABERR_SCALE_C};
            bool  DEBUG_C    = ${if debugC then "true" else "false"};
            float HALF_STRIP = 0.5 * STRIP_W;
            float HALF_SEG   = 0.5 * SEG_H;
            float INV_STRIP_W = ${toString INV_STRIP_W_C};
            float MAX_HALF_H = ${toString MAX_HALF_H_C};
            float MAX_HALF_W = ${toString MAX_HALF_W_C};
            int   SCATTER_MAX = ${toString SCATTER_STRIPS_C};
            float drift_ls = ls * 43.7;
            float tumble_phase_ls = ls * 5.9;
            float tumble_rate_ls = ls * 2.3;
            float flutter_ls = ls * 3.1;

            int nsegs   = int(ceil(sz.y / SEG_H));
            int nstrips = int(ceil(sz.x / STRIP_W));

            // Fall must clear the output bottom (screen space), not just sz.y.
            float off_bottom  = max(niri_output_size.y - niri_window_pos.y, sz.y);
            float fall_gone_y = off_bottom + MAX_HALF_H;

            // Router progress is [0,1] over the full close envelope; stretch into
            // the 1200 ms physics timeline.  Slide uses that base time; fall
            // may scale up so the last segment clears the output bottom at t_prog=1.
            float release_step = ${toString SLIDE_END_C} * SEG_H * inv_sz.y;
            float gi_last      = float(max(nsegs - 1, 0));
            float dt_base      = max(${toString SHREDDER_PHYSICS_SCALE}
                                   - gi_last * release_step, 0.001);
            float fall_base    = -V0_UP * dt_base + GRAVITY_HALF * dt_base * dt_base;
            float needed_fall  = max(fall_gone_y - HALF_SEG, 0.0);
            float fall_time_scale = 1.0;
            if (needed_fall > fall_base) {
                float disc = V0_UP * V0_UP + 4.0 * GRAVITY_HALF * needed_fall;
                float dt_need = (V0_UP + sqrt(disc)) / (2.0 * GRAVITY_HALF);
                float t_fall_end = dt_need + gi_last * release_step;
                fall_time_scale = max(t_fall_end / ${toString SHREDDER_PHYSICS_SCALE}, 1.0);
            }
            float t_slide = t_prog * ${toString SHREDDER_PHYSICS_SCALE};
            float t_fall  = t_slide * fall_time_scale;

            // Precompute slide progress and cut-line (Phase 1 guard).
            float slide_frac = min(t_slide, 1.0) * ${toString INV_SLIDE_END_C};
            float gi_current = slide_frac * sz.y / SEG_H;
            float cut_screen_y = ceil(gi_current) * SEG_H / sz.y - slide_frac;

            // Below the fallen envelope → nothing to render.
            if (px.y > fall_gone_y) return vec4(0.0);

            int nom_si = int(floor(px.x * INV_STRIP_W));
            float nom_strip_x = (float(nom_si) + 0.5) * STRIP_W;

            // Cheap vertical envelope of all released pieces — skip Phase 2 when
            // this scanline cannot intersect any tumbling strip AABB.
            float piece_ymin = 1e30;
            float piece_ymax = -1e30;
            for (int gi = 0; gi < 32; gi++) {
                if (gi >= nsegs) break;
                float t_rel = float(gi) * release_step;
                if (t_slide < t_rel) break;
                float dt = t_fall - t_rel;
                float cy = HALF_SEG + (-V0_UP * dt + GRAVITY_HALF * dt * dt);
                piece_ymin = min(piece_ymin, cy - MAX_HALF_H);
                piece_ymax = max(piece_ymax, cy + MAX_HALF_H);
            }
            bool phase2_might = (px.y >= piece_ymin && px.y <= piece_ymax);

            // ── Phase 2: inverse-transform each released (strip, segment) piece ──
            if (phase2_might) {
            for (int gi = 0; gi < 32; gi++) {
                if (gi >= nsegs) break;
                float t_rel = float(gi) * release_step;
                if (t_slide < t_rel) break;

                float dt       = t_fall - t_rel;
                float fall_y   = -V0_UP * dt + GRAVITY_HALF * dt * dt;
                float fgi      = float(gi);
                float center_y = HALF_SEG + fall_y;
                float pc_orig_y = (fgi + 0.5) * SEG_H;
                float drift_hash_base   = fgi * 311.7 + drift_ls;
                float tumble_phase_base = fgi * 3.7 + tumble_phase_ls;
                float tumble_rate_base  = fgi * 7.9 + tumble_rate_ls;

                if (abs(px.y - center_y) > MAX_HALF_H) continue;

                int dsi_lim = int(ceil(0.5 * DRIFT_MAX * dt * INV_STRIP_W)) + 1;
                dsi_lim = min(dsi_lim, SCATTER_MAX);
                float strip_reach = float(dsi_lim) * STRIP_W + MAX_HALF_W;
                if (abs(px.x - nom_strip_x) > strip_reach) continue;

                for (int dsi = -SCATTER_MAX; dsi <= SCATTER_MAX; dsi++) {
                    if (dsi < -dsi_lim || dsi > dsi_lim) continue;
                    int si = nom_si + dsi;
                    if (si < 0 || si >= nstrips) continue;

                    float fsi = float(si);
                    float strip_center_x = (fsi + 0.5) * STRIP_W;

                    // Per-piece horizontal scatter (3× range), seeded per window via ls.
                    float drift_hash = fract(fsi * 127.1 + drift_hash_base) - 0.5;
                    float fall_x     = drift_hash * DRIFT_MAX * dt;

                    // Y-axis tumble: continuous spin around the strip's vertical axis.
                    // cos(φ) drives horizontal foreshortening; back face (cos<0) is culled.
                    float tumble_phase = fract(fsi * 13.1 + tumble_phase_base) * ${toString TWO_PI_C};
                    float tumble_rate  = ${toString TUMBLE_RATE_BASE_C}
                                       + ${toString TUMBLE_RATE_SCALE_C}
                                         * fract(fsi * 4.1 + tumble_rate_base);
                    float phi          = tumble_rate * dt + tumble_phase;
                    float sin_phi, cos_phi;
                    fast_sincos(wrap_pm_pi(phi), sin_phi, cos_phi);
                    if (cos_phi <= 0.0) continue;          // back-face culling
                    float inv_cos_phi  = 1.0 / max(cos_phi, 0.05);

                    // Z-flutter: secondary wobble in the screen plane.
                    float flutter_phase = ${toString FLUTTER_FREQ_C} * dt
                                        + fract(fsi * 7.3 + flutter_ls);
                    float flutter_sin, flutter_cos;
                    fast_sincos(wrap_pm_pi(flutter_phase), flutter_sin, flutter_cos);
                    float theta         = ${toString FLUTTER_AMP_C} * flutter_sin;
                    float sinT, cosT;
                    fast_sincos(theta, sinT, cosT);

                    // Two piece centres: screen-space (spawn at shredder top) and
                    // texture-space (original window row for this segment).
                    vec2 pc_orig   = vec2(strip_center_x, pc_orig_y);
                    vec2 pc_screen = vec2(strip_center_x + fall_x, center_y);

                    // Cheap wide cull before trig-heavy inverse rotation math.
                    if (abs(px.x - pc_screen.x) > MAX_HALF_W) continue;

                    // Inverse transform: screen px → original pixel position.
                    vec2 rel      = px - pc_screen;
                    float half_w  = HALF_STRIP * inv_cos_phi;
                    float abs_cosT = abs(cosT);
                    float abs_sinT = abs(sinT);
                    float aabb_x   = abs_cosT * half_w + abs_sinT * HALF_SEG;
                    float aabb_y   = abs_sinT * half_w + abs_cosT * HALF_SEG;
                    if (abs(rel.x) > aabb_x || abs(rel.y) > aabb_y) continue;

                    vec2 rot_z    = vec2( cosT * rel.x + sinT * rel.y,
                                         -sinT * rel.x + cosT * rel.y);
                    vec2 orig_rel = vec2(rot_z.x * inv_cos_phi, rot_z.y);
                    vec2 orig     = orig_rel + pc_orig;

                    // Piece bounds check: orig must be inside the original cell rect.
                    float x0 = fsi * STRIP_W,  x1 = x0 + STRIP_W;
                    float y0 = fgi * SEG_H,    y1 = y0 + SEG_H;
                    if (orig.x < x0 || orig.x >= x1 ||
                        orig.y < y0 || orig.y >= y1) continue;

                    // Guard against out-of-window samples (shouldn't happen after bounds
                    // check, but Y-rotation can push orig.x slightly outside).
                    if (orig.x < 0.0 || orig.x >= sz.x ||
                        orig.y < 0.0 || orig.y >= sz.y) return vec4(0.0);

                    vec2 base_uv_geo = orig * inv_sz;

                    if (DEBUG_C) {
                        // Colour each piece by its (strip, segment) hash for geometry debug.
                        vec3 dc = fract(vec3(fsi * 0.1031 + fgi * 7.3  + 1.0,
                                            fsi * 0.0973 + fgi * 11.1 + 2.0,
                                            fsi * 0.0737 + fgi * 4.7  + 3.0));
                        return vec4(dc, 1.0);
                    }

                    // Chromatic aberration scales with both Z-flutter and Y-tumble angle.
                    // UVs are clamped in geo-space before transform to prevent WM bleed.
                    float aberr = (abs_sinT + 0.4 * abs(sin_phi)) * ABERR_SCALE;
                    vec2 uv_r   = vec2(clamp(base_uv_geo.x + aberr, 0.0, 1.0), base_uv_geo.y);
                    vec2 uv_g   = base_uv_geo;
                    vec2 uv_b   = vec2(clamp(base_uv_geo.x - aberr, 0.0, 1.0), base_uv_geo.y);
                    vec3 r_ct   = niri_geo_to_tex * vec3(uv_r, 1.0);
                    vec3 g_ct   = niri_geo_to_tex * vec3(uv_g, 1.0);
                    vec3 b_ct   = niri_geo_to_tex * vec3(uv_b, 1.0);

                    float r = texture(niri_tex, clamp(r_ct.st, 0.0, 1.0)).r;
                    vec4  g_sample = texture(niri_tex, clamp(g_ct.st, 0.0, 1.0));
                    float b = texture(niri_tex, clamp(b_ct.st, 0.0, 1.0)).b;

                    // Whitewash varies per strip over [0, WHITE_MIX].
                    float white_mix = WHITE_MIX * fract(fsi * 19.7 + ls * 23.1);
                    vec3  rgb        = vec3(r, g_sample.g, b);
                    rgb = mix(rgb, vec3(1.0), white_mix);

                    return vec4(rgb, g_sample.a);
                }
            }
            }

            // ── Phase 1: whole window slides upward ───────────────────────────
            // Cut-line guard: released segments must not ghost behind falling pieces.
            if (cg.y < cut_screen_y) return vec4(0.0);

            float sample_y = cg.y + slide_frac;
            if (sample_y > 1.0) return vec4(0.0);   // below window bottom → gone
            vec3 ct  = niri_geo_to_tex * vec3(cg.x, sample_y, 1.0);
            vec4 col = texture(niri_tex, clamp(ct.st, 0.0, 1.0));
            if (DEBUG_C) col = mix(col, vec4(0.2, 0.6, 1.0, 1.0), 0.25);
            return col;
        }
      '';

      voronoi-crumble = ''
        // ── Vertex-baked Voronoi close ───────────────────────────
        //
        // Voronoi field = 16 baked cell-centre vertices (BAKED_CC_NXY) only —
        // no edge-midpoint mesh; gap/edge/owner come from runtime 16-loop nearest-
        // centre metric on those vertices. Same field drives whole-window crack
        // render and per-shard inverse-transform ownership.
        //
        // Phases match Animation B (travel T1, squish T2, static crack T3, crumble).
        // Optimizations vs naive 16×16 ownership:
        //   • cc[16] loaded once; physics precomputed once per fragment
        //   • static crack [T2,T3): single owner + edge, no physics/ownership loops
        //   • crumble: ownership iterates released cells only (dt>0); unreleased
        //     regions fall back to one baked_owner_cell_runtime at q_stat
        vec4 close_voronoi_crumble(vec3 cg, vec3 sg, float t, float s) {
            vec2  sz  = sg.xy;
            vec2  inv_sz = vec2(1.0 / sz.x, 1.0 / sz.y);
            bool  gl  = true;
            float vs_raw = fract(s * 17.3 + 1.5);
            int   variant = baked_voronoi_variant(vs_raw);
            float out_w = max(niri_output_size.x, 1.0);
            bool  explode_mode = (niri_window_pos.x / out_w) >= 0.5;

            float T1 = ${toString T1};
            float T2 = ${toString T2};
            float T3 = ${toString T3};

            // Normalized horizontal throw so window content hits the screen edge:
            // left wall x=0  → travel_end = pos.x / width
            // right wall     → travel_end = (pos.x + width - output.x) / width (≤ 0)
            float inv_wx = 1.0 / sz.x;
            float travel_end = gl
                ? (niri_window_pos.x * inv_wx)
                : ((niri_window_pos.x + sz.x - out_w) * inv_wx);

            float A, B;
            if (t < T1) {
                float tf = t * ${toString invT1};
                A = 1.0;
                B = travel_end * tf;
            } else {
                float p = (t < T2) ? (t - T1) * ${toString invT2T1} : 1.0;
                A = mix(1.0, ${toString halfInvVW}, p);
                B = gl ? travel_end : (1.0 + travel_end - A);
            }
            float gx     = A * cg.x + B;
            vec2  q_stat = vec2(gx * sz.x, cg.y * sz.y);

            if (t < T2) {
                vec2  tc = vec2(gx, cg.y);
                float ib = step(0.0, tc.x) * step(tc.x, 1.0);
                vec3  ct = niri_geo_to_tex * vec3(tc, 1.0);
                return texture(niri_tex, clamp(ct.st, vec2(0.0), vec2(1.0))) * ib;
            }

            vec2  crko   = vec2(0.5 * sz.x, fract(s * 5.7 + 0.3) * sz.y);
            float diag   = length(sz);
            float cov    = fract(s * 2.9 + 0.4) * 0.4 + 0.6;
            float cfr    = explode_mode ? cov * diag
                                        : ((t > T3) ? cov * diag
                                                    : cov * diag * clamp((t - T2) * ${toString invT3T2}, 0.0, 1.0));
            float cfr_sq = cfr * cfr;
            float spin_sign = gl ? 1.0 : -1.0;
            float crumble_y_scale = sz.y * 42.0;
            float explode_gravity = sz.y * 18.0;
            float explode_v0 = diag * 9.0;
            float gap_scale = min(sz.x, sz.y);
            gap_scale *= gap_scale;

            bool explode_static = explode_mode && (t <= T2);
            if (t < T3 && (!explode_mode || explode_static)) {
                vec2  q_norm = q_stat * inv_sz;
                float ib = step(0.0, q_norm.x) * step(q_norm.x, 1.0);

                vec2  q_orig   = q_stat;
                float edge     = voronoi_edge_norm(variant, q_norm) * min(sz.x, sz.y);
                vec2  crack_dv = q_orig - crko;
                float op = 1.0 - 0.7 * step(edge, 1.5)
                                   * step(dot(crack_dv, crack_dv), cfr_sq);
                vec3  ct = niri_geo_to_tex * vec3(q_norm, 1.0);
                return texture(niri_tex, clamp(ct.st, vec2(0.0), vec2(1.0))) * (op * ib);
            }

            // Vertex centres + per-cell physics (merged into one 16-loop).
            vec2  cc[16];
            vec2  co[16];
            float rc[16];
            float rs[16];
            float dt_cell[16];
            float cell_half_x = 0.35 * sz.x;
            float cell_half_y = 0.35 * sz.y;
            // @profile-loop bound=16 effective=16
            for (int i = 0; i < 16; i++) {
                cc[i] = baked_cell_center(variant, i, sz);
                int   rank = explode_mode ? baked_explode_rank(variant, gl, i)
                                          : baked_crumble_rank(variant, gl, i);
                float tr;
                float dt;
                float dt2;
                float ro_val;
                float rv_speed = baked_rv_speed(variant, i);
                float rv_rot   = baked_rv_rot(variant, i);
                float rv_drift = rv_speed;
                if (explode_mode && rank < 12) {
                    int   wave = rank / 4;
                    int   wave_rank = rank - wave * 4;
                    float wave_delay = wave == 0 ? 0.0 : (wave == 1 ? 60.0 : 140.0);
                    float vel_scale  = wave == 0 ? 1.0 : (wave == 1 ? 0.6 : 0.8);
                    tr  = T2 + (wave_delay + float(wave_rank) * 10.0) * ${toString inv1200};
                    dt  = max(t - tr, 0.0);
                    dt2 = dt * dt;

                    vec2  dir = baked_explode_dir(variant, gl, i);
                    bool  use_top = baked_use_top(variant, gl, i);
                    float blast_speed = explode_v0 * vel_scale
                                      * (use_top ? 1.2 : 1.0)
                                      * (0.9 + 0.2 * rv_speed);
                    co[i] = dir * blast_speed * dt + vec2(0.0, explode_gravity * dt2);
                    ro_val = spin_sign * (4.0 + rv_rot * 8.0) * dt;
                } else {
                    float tail_ms = explode_mode ? (140.0 + float(rank - 12) * 25.0)
                                                 : (float(rank) * 25.0 + float(rank / 4) * 75.0);
                    tr  = (explode_mode ? T2 : T3) + tail_ms * ${toString inv1200};
                    dt  = max(t - tr, 0.0);
                    dt2 = dt * dt;
                    float ow_x = baked_crumble_dir(variant, gl, i).x;
                    co[i] = vec2(ow_x * sz.x * (0.4 + 0.5 * rv_drift) * dt,
                                 crumble_y_scale * dt2);
                    ro_val = spin_sign * (1.0 + rv_rot * 3.0) * dt;
                }
                dt_cell[i] = dt;
                fast_sincos(ro_val, rs[i], rc[i]);
            }

            // Vertex ownership: static owner once, then released cells only.
            vec2  q_norm_stat = q_stat * inv_sz;
            int   static_owner = baked_owner_cell_runtime(variant, q_norm_stat);
            vec2  q_orig   = q_stat;
            float best_z   = -1e30;
            float best_gap = 1e30;
            float bwi = -1.0, found = 0.0;
            // @profile-loop bound=16 effective=6
            for (int i = 0; i < 16; i++) {
                if (dt_cell[i] == 0.0) continue;
                if (co[i].y < best_z - 0.5) continue;

                vec2  center = cc[i] + co[i];
                vec2  rel_aabb = q_stat - center;
                float abs_c = abs(rc[i]), abs_s = abs(rs[i]);
                float aabb_x = abs_c * cell_half_x + abs_s * cell_half_y;
                float aabb_y = abs_s * cell_half_x + abs_c * cell_half_y;
                if (abs(rel_aabb.x) > aabb_x || abs(rel_aabb.y) > aabb_y) continue;

                float c  = rc[i], sn = rs[i];
                vec2  rel = q_stat - center;
                vec2  qo  = cc[i] + vec2(c * rel.x + sn * rel.y,
                                         -sn * rel.x + c * rel.y);
                if (qo.x < 0.0 || qo.x > sz.x || qo.y < 0.0 || qo.y > sz.y) continue;

                vec2  q_norm = qo * inv_sz;
                if (baked_owner_cell_runtime(variant, q_norm) != i) continue;
                float gap = voronoi_gap_norm(variant, q_norm) * gap_scale;

                float z     = co[i].y;
                if (z > best_z + 0.5 || (abs(z - best_z) <= 0.5 && gap <= best_gap)) {
                    best_z     = z;
                    best_gap   = gap;
                    q_orig     = qo;
                    bwi        = float(i);
                    found      = 1.0;
                }
            }
            float is_flying = found;
            if (found < 0.5 && dt_cell[static_owner] == 0.0) {
                q_orig = q_stat;
                bwi    = float(static_owner);
                found  = 1.0;
            }

            if (found < 0.5) return vec4(0.0);

            float edge = voronoi_edge_norm(variant, q_orig * inv_sz) * min(sz.x, sz.y);
            vec2  crack_dv = q_orig - crko;
            float op   = 1.0 - 0.7 * step(edge, 1.5)
                                   * step(dot(crack_dv, crack_dv), cfr_sq);

            vec3 ct = niri_geo_to_tex * vec3(q_orig * inv_sz, 1.0);
            vec4 tex_pm = texture(niri_tex, clamp(ct.st, vec2(0.0), vec2(1.0))) * op;
            if (is_flying > 0.5) {
                float ga = ${toString CLOSE_V_GLASS_ALPHA};
                vec4 glass_pm = vec4(vec3(${toString CLOSE_V_GLASS_RGB}) * ga, ga);
                return glass_pm + tex_pm * (1.0 - ga);
            }
            return tex_pm;
        }
      '';
    };

    open = {

      ngon-reveal = ''
        // ── SDF shape reveal ─────────────────────────────────────
        //
        // No voronoi bake needed.
        vec4 ngon_reveal(vec3 coords_geo, vec3 size_geo, float t, float s) {
            // Pixel-space coords centred on window
            vec2 p = (coords_geo.xy - vec2(0.5)) * size_geo.xy;

            // All shapes sized to fully envelop the window at peak
            float max_r    = length(size_geo.xy) * 0.5 * 1.1;
            float expand_t = clamp(t / ${toString OPEN_A_SETTLE_START}, 0.0, 1.0);
            float r        = expand_t * max_r;

            // Rotation 45°–120°, direction CW/CCW — both decorrelated
            // from the shape-band thresholds below via seed hashes
            float max_angle = mix(0.7854, 2.0944, fract(s * 3.7 + 0.2));
            float dir       = sign(fract(s * 7.3 + 0.1) - 0.5);
            float angle = t * max_angle * dir;
            float ca = cos(angle), sa = sin(angle);
            vec2 pr = vec2(ca * p.x + sa * p.y, -sa * p.x + ca * p.y);

            // Shape chosen by seed (5 equal bands).
            // Stars: r*(1.4 + 0.4*cos(n*θ)) — valley=(1.4-0.4)*r=r,
            // so the minimum extent equals max_r (full window coverage).
            float sdf = 0.0;
            if (s < 0.2) {
                // Circle
                sdf = length(pr) - r;
            } else if (s < 0.4) {
                // 3-lobe smooth star (one continuous cosine wave, 3 peaks)
                float theta = atan(pr.y, pr.x);
                sdf = length(pr) - r * (1.4 + 0.4 * cos(3.0 * theta));
            } else if (s < 0.6) {
                // 4-lobe smooth star
                float theta = atan(pr.y, pr.x);
                sdf = length(pr) - r * (1.4 + 0.4 * cos(4.0 * theta));
            } else if (s < 0.8) {
                // 5-lobe smooth star
                float theta = atan(pr.y, pr.x);
                sdf = length(pr) - r * (1.4 + 0.4 * cos(5.0 * theta));
            } else {
                // Pill (vertical capsule) — scaled so horizontal
                // semi-width equals r = max_r (0.45/0.55 ≈ 0.818)
                float hy = r * 0.818;
                vec2 q = pr;
                q.y -= clamp(q.y, -hy, hy);
                sdf = length(q) - r;
            }

            // Hard edge — no feathering
            float mask = step(sdf, 0.0);

            // Trailing ring 30 px inside boundary — ripple wake
            float ring = smoothstep(30.0, 0.0, abs(sdf + 30.0));
            mask *= (1.0 - ring * 0.30);

            // Phase 2: blend shape mask → full window over last 12%
            float blend = smoothstep(${toString OPEN_A_SETTLE_START}, 1.0, t);
            mask = mix(mask, 1.0, blend);

            vec3 coords_tex = niri_geo_to_tex * coords_geo;
            coords_tex.st   = clamp(coords_tex.st, vec2(0.0), vec2(1.0));
            vec4 color = texture(niri_tex, coords_tex.st);
            return color * mask;
        }
      '';

      voronoi-shatter = ''
        vec4 open_v_settle(vec4 col, vec3 cg, float t, float t_settle, float inv_span) {
            if (t < t_settle) return col;
            float w = min((t - t_settle) * inv_span, 1.0);
            vec3  ct = niri_geo_to_tex * cg;
            vec4  full = texture(niri_tex, clamp(ct.st, vec2(0.0), vec2(1.0)));
            return mix(col, full, w);
        }

        const bool  DEBUG_OPEN_V = ${if debugOpenV then "true" else "false"};
        const int   DEBUG_OPEN_V_MODE = ${toString debugOpenVMode};

        // Debug state: 0=hole 1=crack 2=unreleased 3=released-reveal 4=glass
        vec4 open_v_dbg(int state, float t) {
            if (DEBUG_OPEN_V_MODE == 1)
                return vec4(t, fract(t * 8.0), 1.0 - t, 1.0);
            if (DEBUG_OPEN_V_MODE == 2)
                return vec4(fract(float(state) / 16.0), 0.5, 1.0 - fract(float(state) / 16.0), 1.0);
            if (state == 0) return vec4(0.05, 0.05, 0.05, 1.0);
            if (state == 1) return vec4(0.0, 0.85, 1.0, 1.0);
            if (state == 2) return vec4(0.0, 0.75, 0.15, 1.0);
            if (state == 3) return vec4(0.533, 0.533, 0.533, 0.5);
            if (state == 4) return vec4(0.85, 0.0, 0.85, 0.65);
            if (state == 5) return vec4(1.0, 0.55, 0.0, 1.0);
            return vec4(1.0, 0.0, 0.0, 1.0);
        }

        // Flying detached shard — flat translucent gray (#888888 @ 0.5).
        vec4 open_v_flying_shard() {
            float a = ${toString OPEN_V_FLYING_ALPHA};
            vec3  rgb = vec3(${toString OPEN_V_FLYING_RGB});
            return vec4(rgb * a, a);
        }

        // Crack line only: shard interiors stay transparent (opacity 0).
        vec4 open_v_crack_line(vec2 q_norm, vec2 q_stat, vec2 crko, float cfr_sq, int variant, vec2 sz) {
            float edge = voronoi_edge_norm(variant, q_norm) * min(sz.x, sz.y);
            vec2  crack_dv = q_stat - crko;
            if (edge >= 1.5 || dot(crack_dv, crack_dv) >= cfr_sq)
                return vec4(0.0);
            vec3 ct = niri_geo_to_tex * vec3(q_norm, 1.0);
            return texture(niri_tex, clamp(ct.st, vec2(0.0), vec2(1.0))) * ${toString OPEN_V_CRACK_OP};
        }

        // ── Voronoi shatter open (inverse of close_voronoi_crumble) ─
        //
        // Centre crack → outward shatter reveal. No travel/squish. Reuses 16
        // baked cell-centre vertices; ownership iterates released cells only.
        vec4 open_voronoi_shatter(vec3 cg, vec3 sg, float t, float s) {
            vec2  sz      = sg.xy;
            vec2  inv_sz  = vec2(1.0 / sz.x, 1.0 / sz.y);
            vec2  q_stat  = cg.xy * sz;
            vec2  center  = vec2(0.5 * sz.x, 0.5 * sz.y);
            vec2  crko    = center;

            float T_CRACK  = ${toString OPEN_B_T_CRACK_END};
            float T_SETTLE = ${toString OPEN_B_SETTLE_START};
            float INV_SETTLE = ${toString invOpenBSettleSpan};

            if (t >= 1.0) {
                vec3 ct = niri_geo_to_tex * cg;
                return texture(niri_tex, clamp(ct.st, vec2(0.0), vec2(1.0)));
            }

            float vs_raw = fract(s * 17.3 + 1.5);
            int   variant = baked_voronoi_variant(vs_raw);
            bool  explode_mode = fract(s * 3.1 + 0.6) < 0.5;

            float diag   = length(sz);
            float cov    = fract(s * 2.9 + 0.4) * 0.4 + 0.6;
            float cfr    = cov * diag * clamp(t * ${toString invOpenBCrackEnd}, 0.0, 1.0);
            float cfr_sq = cfr * cfr;
            float spin_sign = fract(s * 11.3 + 0.2) < 0.5 ? 1.0 : -1.0;
            float crumble_y_scale = sz.y * 42.0;
            float explode_gravity = sz.y * 18.0;
            float explode_v0 = diag * 9.0;
            float gap_scale = min(sz.x, sz.y);
            gap_scale *= gap_scale;
            float cell_half_y = 0.35 * sz.y;

            bool explode_static = explode_mode && (t <= T_CRACK);
            if ((!explode_mode && t < T_CRACK) || explode_static) {
                vec2  q_norm = q_stat * inv_sz;
                float gap = voronoi_gap_norm(variant, q_norm) * gap_scale;
                if (gap > 1.0) {
                    if (DEBUG_OPEN_V) return open_v_dbg(0, t);
                    return open_v_settle(vec4(0.0), cg, t, T_SETTLE, INV_SETTLE);
                }

                vec2  crack_dv = q_stat - crko;
                if (dot(crack_dv, crack_dv) >= cfr_sq) {
                    if (DEBUG_OPEN_V) return open_v_dbg(0, t);
                    return open_v_settle(vec4(0.0), cg, t, T_SETTLE, INV_SETTLE);
                }

                float edge = voronoi_edge_norm(variant, q_norm) * min(sz.x, sz.y);
                if (edge >= 1.5) {
                    if (DEBUG_OPEN_V) return open_v_dbg(0, t);
                    return open_v_settle(vec4(0.0), cg, t, T_SETTLE, INV_SETTLE);
                }

                vec4 crack_col = open_v_crack_line(q_norm, q_stat, crko, cfr_sq, variant, sz);
                if (DEBUG_OPEN_V) return open_v_dbg(1, t);
                return open_v_settle(crack_col, cg, t, T_SETTLE, INV_SETTLE);
            }

            vec2  cc[16];
            vec2  co[16];
            float rc[16];
            float rs[16];
            float dt_cell[16];
            float cell_half_x = 0.35 * sz.x;
            // @profile-loop bound=16 effective=16
            for (int i = 0; i < 16; i++) {
                cc[i] = baked_cell_center(variant, i, sz);
                int   rank = baked_center_rank(variant, i);
                float tr;
                float dt;
                float dt2;
                float ro_val;
                float rv_speed = baked_rv_speed(variant, i);
                float rv_rot   = baked_rv_rot(variant, i);
                if (explode_mode && rank < 12) {
                    int   wave = rank / 4;
                    int   wave_rank = rank - wave * 4;
                    float wave_delay = wave == 0 ? 0.0 : (wave == 1 ? 60.0 : 140.0);
                    float vel_scale  = wave == 0 ? 1.0 : (wave == 1 ? 0.6 : 0.8);
                    tr  = T_CRACK + (wave_delay + float(wave_rank) * 10.0) * ${toString inv1000};
                    dt  = max(t - tr, 0.0);
                    dt2 = dt * dt;

                    vec2  dir = baked_open_explode_dir(variant, i);
                    vec2  v0 = dir * explode_v0 * vel_scale * (0.9 + 0.2 * rv_speed);
                    co[i] = v0 * dt + vec2(0.0, explode_gravity * dt2);
                    ro_val = spin_sign * (4.0 + rv_rot * 8.0) * dt;
                } else {
                    float tail_ms = explode_mode ? (140.0 + float(rank - 12) * 25.0)
                                                 : 0.0;
                    tr  = explode_mode
                        ? T_CRACK + tail_ms * ${toString inv1000}
                        : T_CRACK + (float(rank) / 15.0) * ${toString OPEN_B_CRUMBLE_SPAN};
                    dt  = max(t - tr, 0.0);
                    dt2 = dt * dt;
                    vec2  dir = baked_open_crumble_dir(variant, i);
                    co[i] = dir * (diag * 0.35 * (0.4 + 0.5 * rv_speed)) * dt
                          + vec2(0.0, crumble_y_scale * dt2);
                    ro_val = spin_sign * (1.0 + rv_rot * 3.0) * dt;
                }
                dt_cell[i] = dt;
                fast_sincos(ro_val, rs[i], rc[i]);
            }

            vec2  q_norm_stat = q_stat * inv_sz;
            int   territory  = baked_owner_cell_runtime(variant, q_norm_stat);

            // Flying-shard competition: inverse rigid-body transform (released only).
            float best_z    = -1e30;
            float shard_hit = 0.0;
            // @profile-loop bound=16 effective=6
            for (int i = 0; i < 16; i++) {
                if (dt_cell[i] == 0.0) continue;
                if (co[i].y < best_z - 0.5) continue;

                vec2  shard_center = cc[i] + co[i];
                vec2  rel_aabb = q_stat - shard_center;
                float abs_c = abs(rc[i]), abs_s = abs(rs[i]);
                float aabb_x = abs_c * cell_half_x + abs_s * cell_half_y;
                float aabb_y = abs_s * cell_half_x + abs_c * cell_half_y;
                if (abs(rel_aabb.x) > aabb_x || abs(rel_aabb.y) > aabb_y) continue;

                float c  = rc[i], sn = rs[i];
                vec2  rel = q_stat - shard_center;
                vec2  qo  = cc[i] + vec2(c * rel.x + sn * rel.y,
                                         -sn * rel.x + c * rel.y);
                if (qo.x < 0.0 || qo.x > sz.x || qo.y < 0.0 || qo.y > sz.y) continue;
                vec2  q_norm = qo * inv_sz;
                if (baked_owner_cell_runtime(variant, q_norm) != i) continue;

                float z = co[i].y;
                if (z > best_z + 0.5) {
                    best_z    = z;
                    shard_hit = 1.0;
                }
            }

            // Flying shards render on top of attached territories.
            if (shard_hit > 0.5) {
                vec4 flying = open_v_flying_shard();
                if (DEBUG_OPEN_V) return open_v_dbg(3, t);
                return open_v_settle(flying, cg, t, T_SETTLE, INV_SETTLE);
            }

            // Attached territory: transparent fill, crack lines only at 0.7.
            if (dt_cell[territory] == 0.0) {
                vec4 attached = open_v_crack_line(q_norm_stat, q_stat, crko, cfr_sq, variant, sz);
                if (DEBUG_OPEN_V)
                    return attached.a > 0.0 ? open_v_dbg(1, t) : open_v_dbg(0, t);
                return open_v_settle(attached, cg, t, T_SETTLE, INV_SETTLE);
            }

            // Territory released, shard gone — full-opacity window revealed.
            vec3  ct_rev = niri_geo_to_tex * vec3(q_norm_stat, 1.0);
            vec4  reveal = texture(niri_tex, clamp(ct_rev.st, vec2(0.0), vec2(1.0)));
            if (DEBUG_OPEN_V) return open_v_dbg(5, t);
            return open_v_settle(reveal, cg, t, T_SETTLE, INV_SETTLE);
        }
      '';
    };

    resize = {
      crt-rip = ''
        // ── Window-Resize: CRT Rip ────────────────────────────────────────────
        // See niri-animations-spec.md § Window-Resize — CRT Rip.

        // Dave Hoskins single-float hash — pure ALU, no trig
        float fhash_r(float n) {
            vec3 p3 = fract(vec3(n) * vec3(0.1031, 0.1030, 0.0973));
            p3 += dot(p3, p3.yzx + 33.33);
            return fract((p3.x + p3.y) * p3.z);
        }

        // Smooth ramp-in / ramp-out envelope [0, 1]
        float crt_envelope(float t) {
            return smoothstep(0.0, ${toString RAMP_IN_R}, t)
                 * (1.0 - smoothstep(${toString RAMP_OUT_R}, 1.0, t));
        }

        // Mild barrel distortion (k > 0 pulls corners inward — classic CRT shape)
        vec2 crt_distort(vec2 uv, float k) {
            vec2 c = uv - 0.5;
            return uv + k * dot(c, c) * c;
        }

        // Window texture sample with horizontal chromatic aberration.
        // aberr_geo: x-offset in geo-space units (px_offset / sz.x).
        // Uses one mat3 multiply then derives R/B offsets in tex-space via the
        // column-0 derivative, avoiding two extra matrix multiplications.
        // Resize shaders use niri_tex_next / niri_geo_to_tex_next (no niri_tex).
        vec4 crt_sample_r(vec2 uv_geo, float aberr_geo) {
            vec3 ct  = niri_geo_to_tex_next * vec3(uv_geo, 1.0);
            vec2 dxy = niri_geo_to_tex_next[0].xy * aberr_geo;
            float r  = texture(niri_tex_next, clamp(ct.st + dxy, 0.0, 1.0)).r;
            vec4  g  = texture(niri_tex_next, clamp(ct.st,       0.0, 1.0));
            float b  = texture(niri_tex_next, clamp(ct.st - dxy, 0.0, 1.0)).b;
            return vec4(r, g.g, b, g.a);
        }

        // Hard-edge horizontal scanlines (1 = full brightness, < 1 = dark band)
        float crt_scanline_r(float py) {
            float phase = fract(py * ${toString INV_SCANLINE_R});
            return 1.0 - ${toString SCANLINE_DEPTH_R} * step(phase, 0.5);
        }

        // Sawtooth brightness flicker — no trig, bounded ±FLICKER_AMP around 1.0
        float crt_flicker_r(float t, float seed) {
            float phase = fract(seed * 41.7 + t * ${toString FLICKER_FREQ_R});
            return 1.0 + ${toString FLICKER_AMP_R} * (2.0 * phase - 1.0);
        }

        // Digital glitch: random horizontal displacement for scanline bands.
        // Bands of ~8 px switch state ~12× per t-unit; ~18% are shifted at any tick.
        // Returns geo-space x-offset (signed fraction of window width).
        float crt_glitch_shift_r(float py, float t, float seed) {
            float band = floor(py * ${toString INV_GLITCH_BAND_H_R});
            float tick = floor(t * ${toString GLITCH_TICK_RATE_R});
            float act  = fhash_r(seed * 31.7 + band * 53.1 + tick * 7.3);
            if (act > ${toString GLITCH_ACT_PROB_R}) return 0.0;
            float mag  = fhash_r(seed * 17.3 + band * 23.7 + tick * 11.1);
            return (mag * 2.0 - 1.0) * ${toString GLITCH_MAX_SHIFT_R};
        }

        // True when tear slot i is within its stochastic active window
        bool tear_active_r(float i, float t, float seed) {
            float onset = ${toString TEAR_ONSET_MIN_R}
                        + fhash_r(seed * 7.3 + i * 19.1) * ${toString TEAR_ONSET_JITTER_R};
            float span  = ${toString TEAR_SPAN_MIN_R}
                        + fhash_r(seed * 3.7 + i * 37.3) * ${toString TEAR_SPAN_JITTER_R};
            return t >= onset && t < onset + span;
        }

        // Derive geometry for noisy glitch tear slot.
        // Tears are narrow horizontal bands centered at (cx, cy).
        void get_tear_geom_r(float slot, float seed, vec2 sz,
                             out float cy, out float half_h,
                             out float cx, out float half_w) {
            float h_cy = fhash_r(seed * 11.9 + slot * 53.1);
            float h_hh = fhash_r(seed * 17.7 + slot * 71.3);
            float h_cx = fhash_r(seed * 29.3 + slot * 37.9);
            float h_hw = fhash_r(seed * 43.7 + slot * 61.3);
            cy     = sz.y * (${toString TEAR_MARGIN_R}
                             + h_cy * (1.0 - 2.0 * ${toString TEAR_MARGIN_R}));
            half_h = sz.y * (${toString TEAR_BASE_HMIN_F_R}
                             + h_hh * (${toString TEAR_BASE_HMAX_F_R}
                                       - ${toString TEAR_BASE_HMIN_F_R}));
            cx     = sz.x * (${toString TEAR_CX_MARGIN_R}
                             + h_cx * (1.0 - 2.0 * ${toString TEAR_CX_MARGIN_R}));
            half_w = sz.x * (${toString TEAR_HW_MIN_R}
                             + h_hw * (${toString TEAR_HW_MAX_R} - ${toString TEAR_HW_MIN_R}));
        }

        // Noisy glitch tear: per-8px-scanline band activation with ragged H extents.
        // Each band flickers in/out independently and varies its own left/right edges
        // each tick — no clean geometric boundary, pure hash-driven digital noise.
        bool glitch_tear_hit_r(vec2 px, float cy, float half_h,
                                float cx, float half_w,
                                float t, float seed, float slot) {
            float vert_dist = abs(px.y - cy);
            if (vert_dist > half_h * 3.5) return false;
            float band    = floor(px.y * ${toString INV_GLITCH_BAND_H_R});
            float tick    = floor(t * ${toString GLITCH_TICK_RATE_R});
            float h_act   = fhash_r(seed * 41.3 + slot * 17.1 + band * 53.7 + tick * 3.1);
            // ~80% activate at core; probability drops to 0 at half_h radius.
            float vert_norm = clamp(vert_dist / half_h, 0.0, 1.0);
            if (h_act > 0.80 * (1.0 - vert_norm)) return false;
            // Ragged per-band horizontal extents, re-randomised each tick.
            float h_l    = fhash_r(seed * 61.7 + slot * 23.9 + band * 71.1 + tick * 5.3);
            float h_r    = fhash_r(seed * 83.1 + slot * 37.7 + band * 89.3 + tick * 7.1);
            float left_x  = cx - half_w * (0.25 + h_l);
            float right_x = cx + half_w * (0.25 + h_r);
            return px.x >= left_x && px.x <= right_x;
        }

        // Matrix rain — scrolling green glyphs on black.
        // Columns are indexed by absolute window x so the rain layer is independent
        // of tear position (the tear "reveals" a background layer).
        vec4 matrix_rain_r(vec2 px, vec2 sz, float t) {
            float col_i   = floor(px.x * ${toString INV_RAIN_COL_W_R});
            float phase   = fhash_r(col_i * 17.3) * 60.0;
            float scroll  = t * ${toString RAIN_SCROLL_SPD_R} + phase;
            float local_y = mod(px.y - scroll, sz.y);
            float in_trail = step(local_y, ${toString RAIN_TRAIL_LEN_R});
            float bright   = max(0.0, 1.0 - local_y * ${toString INV_RAIN_TRAIL_R}) * in_trail;
            float row_i    = floor(local_y * ${toString INV_RAIN_GLYPH_H_R});
            float glyph_m  = 0.55 + 0.45 * fhash_r(col_i * 31.7 + row_i * 157.3);
            float head_fac = step(local_y, ${toString RAIN_GLYPH_H_R}) * in_trail;
            vec3  base_col = mix(vec3(0.0, 0.82, 0.10) * glyph_m,
                                 vec3(0.85, 1.0, 0.85), head_fac);
            return vec4(base_col * bright, 1.0);
        }

        // ── Entry point ───────────────────────────────────────────────────────
        vec4 resize_crt_distort(vec3 coords_geo, vec3 size_geo) {
            float t    = niri_clamped_progress;
            // Resize shaders have no niri_random_seed; derive a stable per-resize
            // seed from the next-texture transform (encodes the final window content
            // position and is constant throughout the animation).
            float seed = fhash_r(niri_geo_to_tex_next[0].x * 73.1
                                + niri_geo_to_tex_next[1].y * 41.3);
            float e    = crt_envelope(t);
            vec2  px   = coords_geo.xy * size_geo.xy;
            vec2  uv   = coords_geo.xy;

            // CRT base: barrel distort → glitch shift → chromatic aberration → scanlines → flicker
            vec2  uv_d     = clamp(crt_distort(uv, ${toString BARREL_K_R} * e), 0.0, 1.0);
            float glitch_x = crt_glitch_shift_r(px.y, t, seed) * e;
            vec2  uv_g     = clamp(uv_d + vec2(glitch_x, 0.0), 0.0, 1.0);
            float aberr    = ${toString RGB_SPLIT_R} * e / size_geo.x
                           + abs(glitch_x) * ${toString GLITCH_ABERR_MUL_R};
            vec4  crt      = crt_sample_r(uv_g, aberr);
            crt.rgb       *= crt_scanline_r(px.y) * crt_flicker_r(t, seed);

            // Plain cross-faded sample — blend target at e=0 (ramp edges).
            // Resize shaders have prev (old size) and next (new size) textures;
            // cross-fade between them so ramp edges show a smooth size transition.
            vec3 ct_prev  = niri_geo_to_tex_prev * coords_geo;
            vec3 ct_next  = niri_geo_to_tex_next * coords_geo;
            vec4 plain    = mix(
                texture(niri_tex_prev, clamp(ct_prev.st, 0.0, 1.0)),
                texture(niri_tex_next, clamp(ct_next.st, 0.0, 1.0)),
                t
            );

            // e=0 → plain window, e=1 → full CRT effect
            vec4 col = mix(plain, crt, e);

            // Tear pass — 3 slots unrolled (no SPIR-V loop, no convergence overhead)
            float cy = 0.0, half_h = 0.0, cx = 0.0, half_w = 0.0;

            if (tear_active_r(0.0, t, seed)) {
                get_tear_geom_r(0.0, seed, size_geo.xy, cy, half_h, cx, half_w);
                if (glitch_tear_hit_r(px, cy, half_h, cx, half_w, t, seed, 0.0))
                    return matrix_rain_r(px, size_geo.xy, t);
            }
            if (tear_active_r(1.0, t, seed)) {
                get_tear_geom_r(1.0, seed, size_geo.xy, cy, half_h, cx, half_w);
                if (glitch_tear_hit_r(px, cy, half_h, cx, half_w, t, seed, 1.0))
                    return matrix_rain_r(px, size_geo.xy, t);
            }
            if (tear_active_r(2.0, t, seed)) {
                get_tear_geom_r(2.0, seed, size_geo.xy, cy, half_h, cx, half_w);
                if (glitch_tear_hit_r(px, cy, half_h, cx, half_w, t, seed, 2.0))
                    return matrix_rain_r(px, size_geo.xy, t);
            }

            return col;
        }
      '';
    };
  };

  # ── Shader profiles ───────
  # defaulted probability for those unspecified (remaining space / n unspecified)
  simple = {
    openSharedPreamble = "";
    open = [
      {
        fn = "ngon_reveal";
        durationMs = DUR_OPEN_NGON;
        needsVoronoiBake = false;
        body = animations.open.ngon-reveal;
      }
    ];
    resizeSharedPreamble = "";
    resize = [
      {
        fn = "resize_crt_distort";
        durationMs = DUR_RESIZE_CRT;
        needsVoronoiBake = false;
        body = animations.resize.crt-rip;
      }
    ];
    closeSharedPreamble = fastSincosPreamble;
    close = [
      {
        fn = "close_unhook_fall";
        durationMs = DUR_CLOSE_UNHOOK;
        needsVoronoiBake = false;
        body = animations.close.unhook-fall;
      }
      {
        prob = 0.5;
        fn = "close_shredder";
        durationMs = DUR_CLOSE_SHREDDER;
        needsVoronoiBake = false;
        body = animations.close.shredder;
        forceWhen = "niri_is_tabbed == 1.0 && niri_windows_in_column > 1.0";
      }
    ];
  };

  full = {
    openSharedPreamble = fastSincosPreamble + voronoiRuntimePreamble;
    open = [
      {
        prob = 0.45;
        fn = "ngon_reveal";
        durationMs = DUR_OPEN_NGON;
        needsVoronoiBake = false;
        body = animations.open.ngon-reveal;
      }
    ] ++ [
      {
        fn = "open_voronoi_shatter";
        durationMs = DUR_OPEN_VORONOI;
        needsVoronoiBake = true;
        body = animations.open.voronoi-shatter;
        includeWhen = "(niri_window_size.x / max(niri_output_size.x, 1.0)) < 0.7";
      }
    ];
    resizeSharedPreamble = simple.resizeSharedPreamble;
    resize = simple.resize;
    closeSharedPreamble = fastSincosPreamble + voronoiRuntimePreamble;
    close = simple.close ++ [
      {
        fn = "close_voronoi_crumble";
        durationMs = DUR_CLOSE_VORONOI;
        needsVoronoiBake = true;
        body = animations.close.voronoi-crumble;
        includeWhen = "(niri_window_size.x / max(niri_output_size.x, 1.0)) < 0.7";
      }
    ];
  };
in
{
  inherit simple full;
}
