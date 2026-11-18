'use strict';
/*
 * MapStash's background piano: an endless, original, gently changing piece,
 * synthesized live with the Web Audio API (no audio files).
 *
 * Key of D major, ~66 BPM. Each bar has a soft bass note, a flowing left-hand
 * arpeggio, and a sparse right-hand melody that wanders through the scale.
 * Chord progressions are picked at random every 4 bars.
 */

const Piano = (() => {
  const mtof = (m) => 440 * Math.pow(2, (m - 69) / 12);
  const rand = (a, b) => a + Math.random() * (b - a);
  const pick = (arr) => arr[Math.floor(Math.random() * arr.length)];

  // Chords as MIDI pitch classes in D major (D=2). Names are just for reading.
  const CH = {
    Dmaj7: [2, 6, 9, 1], Bm9: [11, 2, 6, 9, 1], Gmaj7: [7, 11, 2, 6], A6: [9, 1, 4, 6],
    Fsm7: [6, 9, 1, 4], Em9: [4, 7, 11, 2, 6], Asus: [9, 2, 4, 7], Gadd9: [7, 11, 2, 9],
  };
  const PROGRESSIONS = [
    ['Gmaj7', 'A6', 'Fsm7', 'Bm9'],
    ['Dmaj7', 'Bm9', 'Gmaj7', 'Asus'],
    ['Em9', 'A6', 'Dmaj7', 'Bm9'],
    ['Gmaj7', 'Fsm7', 'Em9', 'Asus'],
    ['Bm9', 'Gadd9', 'Dmaj7', 'A6'],
    ['Gadd9', 'A6', 'Dmaj7', 'Dmaj7'],
  ];
  const SCALE = [2, 4, 6, 9, 11]; // D major pentatonic: D E F# A B (melody)
  const ARP = [0, 1, 2, 3, 2, 1, 2, 3]; // left-hand pattern over 8 eighth notes
  const RHYTHMS = [ // right-hand rhythms in beats; negative = rest
    [1.5, 0.5, 2], [2, 2], [1, 1, 2], [4], [-1, 1, 2], [-2, 1.5, 0.5], [0.5, 0.5, 1, 2], [3, 1],
  ];

  function impulse(ctx, seconds = 3.2, decay = 2.6) {
    const len = Math.floor(ctx.sampleRate * seconds);
    const buf = ctx.createBuffer(2, len, ctx.sampleRate);
    for (let ch = 0; ch < 2; ch++) {
      const d = buf.getChannelData(ch);
      for (let i = 0; i < len; i++) d[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / len, decay);
    }
    return buf;
  }

  class Engine {
    /* out: the node to connect to (a gain the caller controls for volume). */
    constructor(ctx, out) {
      this.ctx = ctx;
      this.bus = ctx.createGain();
      this.bus.gain.value = 0.8;
      const tone = ctx.createBiquadFilter();
      tone.type = 'lowpass';
      tone.frequency.value = 3200;
      tone.Q.value = 0.4;
      const dry = ctx.createGain();
      dry.gain.value = 0.72;
      const verb = ctx.createConvolver();
      verb.buffer = impulse(ctx);
      const wet = ctx.createGain();
      wet.gain.value = 0.42;
      const comp = ctx.createDynamicsCompressor();
      comp.threshold.value = -20;
      comp.ratio.value = 3;
      this.bus.connect(tone);
      tone.connect(dry).connect(comp);
      tone.connect(verb).connect(wet).connect(comp);
      comp.connect(out);
      this.reset();
    }

    reset() {
      this.bar = 0;
      this.prog = PROGRESSIONS[0];
      this.melody = 74; // D5
      this.nextBar = null;
      this.timer = null;
    }

    get beat() { return 60 / 66; }

    note(midi, t, vel, length) {
      const ctx = this.ctx;
      const f = mtof(midi);
      const decay = Math.min(5.5, 1.8 + (88 - midi) / 18); // low notes ring longer
      const end = t + Math.min(decay, length + 1.6);
      const partials = [[1, 1], [2, 0.42], [3, 0.16], [4, 0.07], [5, 0.03]];
      for (const [n, amp] of partials) {
        const o = ctx.createOscillator();
        o.type = 'sine';
        o.frequency.value = f * n * (1 + 0.00035 * n * n); // slight piano-like stretch
        o.detune.value = rand(-3, 3);
        const g = ctx.createGain();
        const peak = vel * amp * 0.5;
        const d = decay / Math.sqrt(n);
        g.gain.setValueAtTime(0, t);
        g.gain.linearRampToValueAtTime(peak, t + 0.005);
        g.gain.exponentialRampToValueAtTime(Math.max(peak * 0.35, 1e-5), t + 0.18);
        g.gain.exponentialRampToValueAtTime(1e-5, t + d);
        g.gain.setTargetAtTime(0, end, 0.15); // soft release
        o.connect(g).connect(this.bus);
        o.start(t);
        o.stop(Math.min(t + d, end + 0.6) + 0.05);
      }
    }

    voice(pcs, lo, hi) {
      // Chord tones between lo and hi, low to high.
      const out = [];
      for (let m = lo; m <= hi; m++) if (pcs.includes(((m % 12) + 12) % 12)) out.push(m);
      return out;
    }

    scheduleBar(t) {
      const b = this.beat;
      if (this.bar % 4 === 0) this.prog = this.bar === 0 ? PROGRESSIONS[0] : pick(PROGRESSIONS);
      const chord = CH[this.prog[this.bar % 4]];
      const h = () => rand(-0.012, 0.012); // human timing

      // bass: root, held
      const root = 38 + ((chord[0] - 2 + 12) % 12); // D2..C#3
      this.note(root, t + Math.max(0, h()), rand(0.42, 0.5), 4 * b);
      if (Math.random() < 0.5) this.note(root + 12, t + 2 * b + h(), 0.22, 2 * b);

      // left-hand arpeggio
      const arp = this.voice(chord, 50, 66).slice(0, 4);
      while (arp.length < 4) arp.push(arp[arp.length - 1] + 12);
      ARP.forEach((idx, i) => {
        if (i === 0 && Math.random() < 0.3) return; // breathe sometimes
        this.note(arp[idx], t + i * b / 2 + h(), rand(0.16, 0.24) * (i % 2 ? 0.85 : 1), b);
      });

      // right-hand melody (rests some bars)
      if (Math.random() < 0.2 && this.bar % 4 !== 0) { this.bar++; return; }
      const scale = this.voice(SCALE, 66, 83);
      const tones = this.voice(chord, 66, 83);
      let pos = 0;
      for (const len of pick(RHYTHMS)) {
        const dur = Math.abs(len) * b;
        if (len > 0) {
          const strong = pos % 2 === 0;
          let i = scale.indexOf(this.melody);
          if (i < 0) {
            i = scale.findIndex((m) => m >= this.melody);
            if (i < 0) i = scale.length - 1;
          }
          i = Math.max(0, Math.min(scale.length - 1, i + pick([-2, -1, -1, 0, 1, 1, 2])));
          let m = scale[i];
          if (strong && tones.length) m = tones.reduce((a, c) => (Math.abs(c - m) < Math.abs(a - m) ? c : a), tones[0]);
          if (m > 81) m -= 12;
          this.melody = m;
          this.note(m, t + pos * b + h(), rand(0.32, 0.44) * (strong ? 1 : 0.85), dur);
        }
        pos += Math.abs(len);
      }
      this.bar++;
    }

    /* Schedule every bar that starts before `until` (seconds on the ctx clock). */
    scheduleUntil(until) {
      if (this.nextBar == null) this.nextBar = this.ctx.currentTime + 0.1;
      while (this.nextBar < until) {
        this.scheduleBar(this.nextBar);
        this.nextBar += 4 * this.beat;
      }
    }

    start() {
      if (this.timer) return;
      this.nextBar = this.ctx.currentTime + 0.15;
      this.timer = setInterval(() => this.scheduleUntil(this.ctx.currentTime + 1.5), 250);
      this.scheduleUntil(this.ctx.currentTime + 1.5);
    }

    stop() {
      clearInterval(this.timer);
      this.timer = null;
      this.nextBar = null;
    }
  }

  return { Engine };
})();
