import * as THREE from 'three';
import { OrbitControls } from './vendor/three-r160/examples/jsm/controls/OrbitControls.js';
import { STLLoader } from './vendor/three-r160/examples/jsm/loaders/STLLoader.js';

export class ModelViewer {
  constructor(container) {
    this.container = container;
    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color('#18281f');
    this.camera = new THREE.PerspectiveCamera(45, 1, 0.01, 100000);
    this.camera.up.set(0, 0, 1);
    this.renderer = new THREE.WebGLRenderer({antialias: true});
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.domElement.setAttribute('aria-label', 'Interactive STL model: drag to rotate, scroll to zoom');
    container.append(this.renderer.domElement);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.addEventListener('change', () => this.render());
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x567364, 2.5));
    const light = new THREE.DirectionalLight(0xffffff, 3);
    light.position.set(50, -30, 100);
    this.scene.add(light);
    this.observer = new ResizeObserver(() => this.resize());
    this.observer.observe(container);
    this.resize();
  }

  render() { this.renderer.render(this.scene, this.camera); }

  resize() {
    const width = Math.max(this.container.clientWidth, 1);
    const height = Math.max(this.container.clientHeight, 1);
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(width, height, false);
    this.render();
  }

  clear() {
    this.size = null;
    delete this.renderer.domElement.dataset.meshLoaded;
    for (const name of ['mesh', 'grid', 'axes']) {
      const item = this[name];
      if (item) {
        this.scene.remove(item);
        item.geometry.dispose();
        for (const material of [].concat(item.material)) material.dispose();
        this[name] = null;
      }
    }
    this.render();
  }

  async load(url, signal) {
    const response = await fetch(url, {cache: 'no-store', signal});
    if (!response.ok) throw new Error('STL download failed');
    const geometry = new STLLoader().parse(await response.arrayBuffer());
    geometry.computeBoundingBox();
    const size = geometry.boundingBox.getSize(new THREE.Vector3());
    if (![size.x, size.y, size.z].every(Number.isFinite) || Math.max(size.x, size.y, size.z) <= 0) {
      geometry.dispose();
      throw new Error('STL has no measurable geometry');
    }
    if (signal.aborted) { geometry.dispose(); return null; }
    this.clear();
    geometry.center();
    geometry.computeVertexNormals();
    this.mesh = new THREE.Mesh(geometry, new THREE.MeshStandardMaterial({color: '#70dca0', roughness: .48, metalness: .08}));
    this.scene.add(this.mesh);
    this.size = size;
    const span = Math.max(size.x, size.y, size.z);
    this.grid = new THREE.GridHelper(span * 1.6, 10, '#739c83', '#385743');
    this.grid.rotation.x = Math.PI / 2;
    this.grid.position.z = -size.z / 2 - span * .002;
    this.scene.add(this.grid);
    this.axes = new THREE.AxesHelper(span * .65);
    this.axes.position.set(-span*.7, -span*.7, -size.z/2);
    this.scene.add(this.axes);
    this.fit();
    this.renderer.domElement.dataset.meshLoaded = 'true';
    return {x: size.x, y: size.y, z: size.z};
  }

  fit() {
    if (!this.size) return;
    const span = Math.max(this.size.x, this.size.y, this.size.z);
    const distance = span / Math.tan(THREE.MathUtils.degToRad(22.5)) * Math.max(1, 1/this.camera.aspect);
    this.camera.near = Math.max(span / 10000, .0001);
    this.camera.far = Math.max(span * 1000, 100);
    this.camera.position.set(distance * .8, -distance * .8, distance * .65);
    this.controls.target.set(0, 0, 0);
    this.controls.minDistance = span * .05;
    this.controls.maxDistance = span * 100;
    this.camera.updateProjectionMatrix();
    this.controls.update();
    this.render();
  }
}
