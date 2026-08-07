#!/usr/bin/env node
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
await import(path.resolve(here, '..', 'src', 'server.js'));
