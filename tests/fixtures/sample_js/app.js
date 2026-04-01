export class EventEmitter {
  constructor() {
    this.listeners = {};
  }

  on(event, callback) {
    if (!this.listeners[event]) {
      this.listeners[event] = [];
    }
    this.listeners[event].push(callback);
  }

  emit(event, data) {
    const callbacks = this.listeners[event] || [];
    callbacks.forEach(cb => cb(data));
  }
}

export class AppController extends EventEmitter {
  constructor(config) {
    super();
    this.config = config;
    this.services = {};
  }

  registerService(name, service) {
    this.services[name] = service;
    this.emit('serviceRegistered', { name });
  }

  start() {
    const server = new HttpServer(this.config.port);
    server.listen();
    this.emit('started', {});
  }
}

class HttpServer {
  constructor(port) {
    this.port = port;
  }

  listen() {
    console.log(`Listening on port ${this.port}`);
  }

  close() {
    console.log('Server closed');
  }
}

