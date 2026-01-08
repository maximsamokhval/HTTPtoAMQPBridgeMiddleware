#!/usr/bin/env python3
"""
EDI Topology Initialization Script

This script sets up the RabbitMQ topology for EDI (Electronic Data Interchange)
based on Enterprise Integration Patterns (EIP):

Topology Overview:
==================
Exchanges:
- edi.internal.topic   : Primary message router (Topic Exchange)
- edi.deadletter.topic : Dead Letter Exchange for failed messages
- edi.retry.topic      : Retry exchange with TTL queues

Routing Key Format:
{source}.{target}.{domain}.{type}.{action}.{version}

Examples:
- erp_central.wh_lviv.logistics.despatch_advice.created.v1
- acc_kyiv.all.finance.invoice.created.v1

Queue Isolation:
Each system has its own isolated inbox queue with DLX configured.

Usage:
    python scripts/init_topology.py

Environment Variables:
    RABBITMQ_URL - AMQP connection URL (default: amqp://guest:guest@localhost:5672/edi)
"""

import asyncio
import os
import sys

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import aio_pika
from aio_pika import ExchangeType


# Topology Configuration
EXCHANGES = [
    {
        "name": "edi.internal.topic",
        "type": ExchangeType.TOPIC,
        "durable": True,
        "description": "Primary message router - Topic Exchange for EDA"
    },
    {
        "name": "edi.deadletter.topic", 
        "type": ExchangeType.TOPIC,
        "durable": True,
        "description": "Dead Letter Exchange for failed/rejected messages"
    },
    {
        "name": "edi.retry.topic",
        "type": ExchangeType.TOPIC,
        "durable": True,
        "description": "Retry exchange for delayed reprocessing"
    },
]

# Queue definitions with DLX and TTL settings
# Message TTL: 7 days (604800000ms)
QUEUES = [
    # System inbox queues
    {
        "name": "q.erp_central.inbox",
        "durable": True,
        "arguments": {
            "x-dead-letter-exchange": "edi.deadletter.topic",
            "x-message-ttl": 604800000,  # 7 days
        },
        "description": "ERP Central system inbox"
    },
    {
        "name": "q.acc_kyiv.inbox",
        "durable": True,
        "arguments": {
            "x-dead-letter-exchange": "edi.deadletter.topic",
            "x-message-ttl": 604800000,
        },
        "description": "Accounting Kyiv system inbox"
    },
    {
        "name": "q.wh_lviv.inbox",
        "durable": True,
        "arguments": {
            "x-dead-letter-exchange": "edi.deadletter.topic",
            "x-message-ttl": 604800000,
        },
        "description": "Warehouse Lviv system inbox"
    },
    {
        "name": "q.logistics.inbox",
        "durable": True,
        "arguments": {
            "x-dead-letter-exchange": "edi.deadletter.topic",
            "x-message-ttl": 604800000,
        },
        "description": "Logistics system inbox (despatch_advice only)"
    },
    # Dead letter queue
    {
        "name": "q.deadletter",
        "durable": True,
        "arguments": {},
        "description": "Dead letter queue for failed messages"
    },
    # Retry queues with TTL
    {
        "name": "q.retry.5s",
        "durable": True,
        "arguments": {
            "x-dead-letter-exchange": "edi.internal.topic",
            "x-message-ttl": 5000,  # 5 seconds
        },
        "description": "Retry queue - 5 second delay"
    },
    {
        "name": "q.retry.30s",
        "durable": True,
        "arguments": {
            "x-dead-letter-exchange": "edi.internal.topic",
            "x-message-ttl": 30000,  # 30 seconds
        },
        "description": "Retry queue - 30 second delay"
    },
    {
        "name": "q.retry.5m",
        "durable": True,
        "arguments": {
            "x-dead-letter-exchange": "edi.internal.topic",
            "x-message-ttl": 300000,  # 5 minutes
        },
        "description": "Retry queue - 5 minute delay"
    },
]

# Bindings: source exchange -> queue with routing key
BINDINGS = [
    # ERP Central receives: direct messages + broadcast
    {"exchange": "edi.internal.topic", "queue": "q.erp_central.inbox", "routing_key": "*.erp_central.#"},
    {"exchange": "edi.internal.topic", "queue": "q.erp_central.inbox", "routing_key": "*.all.#"},
    
    # Accounting Kyiv receives: direct messages + finance broadcast
    {"exchange": "edi.internal.topic", "queue": "q.acc_kyiv.inbox", "routing_key": "*.acc_kyiv.#"},
    {"exchange": "edi.internal.topic", "queue": "q.acc_kyiv.inbox", "routing_key": "*.all.finance.#"},
    
    # Warehouse Lviv receives: direct messages + logistics broadcast
    {"exchange": "edi.internal.topic", "queue": "q.wh_lviv.inbox", "routing_key": "*.wh_lviv.#"},
    {"exchange": "edi.internal.topic", "queue": "q.wh_lviv.inbox", "routing_key": "*.all.logistics.#"},
    
    # Logistics system receives: despatch_advice from anywhere
    {"exchange": "edi.internal.topic", "queue": "q.logistics.inbox", "routing_key": "#.despatch_advice.#"},
    {"exchange": "edi.internal.topic", "queue": "q.logistics.inbox", "routing_key": "#.logistics.#"},
    
    # Dead letter queue catches all DLX messages
    {"exchange": "edi.deadletter.topic", "queue": "q.deadletter", "routing_key": "#"},
    
    # Retry bindings
    {"exchange": "edi.retry.topic", "queue": "q.retry.5s", "routing_key": "retry.5s.#"},
    {"exchange": "edi.retry.topic", "queue": "q.retry.30s", "routing_key": "retry.30s.#"},
    {"exchange": "edi.retry.topic", "queue": "q.retry.5m", "routing_key": "retry.5m.#"},
]


async def init_topology(rabbitmq_url: str) -> None:
    """Initialize EDI topology in RabbitMQ."""
    print(f"Connecting to RabbitMQ: {rabbitmq_url.split('@')[-1]}")  # Hide password
    
    connection = await aio_pika.connect_robust(rabbitmq_url)
    
    async with connection:
        channel = await connection.channel()
        
        # Create exchanges
        print("\n=== Creating Exchanges ===")
        exchanges = {}
        for ex_config in EXCHANGES:
            print(f"  ✓ {ex_config['name']} ({ex_config['type'].value})")
            exchanges[ex_config['name']] = await channel.declare_exchange(
                ex_config['name'],
                ex_config['type'],
                durable=ex_config['durable'],
            )
        
        # Create queues
        print("\n=== Creating Queues ===")
        queues = {}
        for q_config in QUEUES:
            print(f"  ✓ {q_config['name']}")
            if q_config.get('arguments'):
                args = q_config['arguments']
                if 'x-dead-letter-exchange' in args:
                    print(f"      DLX: {args['x-dead-letter-exchange']}")
                if 'x-message-ttl' in args:
                    ttl_seconds = args['x-message-ttl'] / 1000
                    if ttl_seconds >= 86400:
                        print(f"      TTL: {ttl_seconds / 86400:.0f} days")
                    elif ttl_seconds >= 60:
                        print(f"      TTL: {ttl_seconds / 60:.0f} minutes")
                    else:
                        print(f"      TTL: {ttl_seconds:.0f} seconds")
            
            queues[q_config['name']] = await channel.declare_queue(
                q_config['name'],
                durable=q_config['durable'],
                arguments=q_config.get('arguments', {}),
            )
        
        # Create bindings
        print("\n=== Creating Bindings ===")
        for binding in BINDINGS:
            print(f"  ✓ {binding['exchange']} -> {binding['queue']}")
            print(f"      routing_key: {binding['routing_key']}")
            await queues[binding['queue']].bind(
                exchanges[binding['exchange']],
                routing_key=binding['routing_key'],
            )
        
        print("\n=== Topology Initialization Complete ===")
        print(f"  Exchanges: {len(EXCHANGES)}")
        print(f"  Queues: {len(QUEUES)}")
        print(f"  Bindings: {len(BINDINGS)}")


import json
import urllib.request
import urllib.error
from urllib.parse import urlparse

async def ensure_vhost_exists(rabbitmq_url: str):
    """Ensure the target vhost exists using RabbitMQ Management API."""
    parsed = urlparse(rabbitmq_url)
    
    # Defaults and parsing
    host = parsed.hostname or "localhost"
    # Assume management port is 15672 if AMQP is 5672, or use default
    mgmt_port = 15672 
    username = parsed.username or "guest"
    password = parsed.password or "guest"
    vhost = parsed.path.lstrip("/")
    
    if not vhost:
        print("Using default vhost '/', skipping creation.")
        return

    print(f"Checking vhost '{vhost}'...")
    
    # Base URL for Management API
    base_url = f"http://{host}:{mgmt_port}/api"
    
    # Auth handler
    password_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    password_mgr.add_password(None, base_url, username, password)
    handler = urllib.request.HTTPBasicAuthHandler(password_mgr)
    opener = urllib.request.build_opener(handler)
    
    # 1. Create vhost via PUT /api/vhosts/{name}
    vhost_url = f"{base_url}/vhosts/{vhost}"
    try:
        req = urllib.request.Request(vhost_url, method='PUT')
        with opener.open(req) as response:
            if response.status in (201, 204):
                print(f"  ✓ Vhost '{vhost}' created/verified.")
    except urllib.error.HTTPError as e:
        print(f"  ⚠ Config warning: Could not create vhost (HTTP {e.code}). Attempting to connect anyway...")
    except Exception as e:
        print(f"  ⚠ Config warning: Could not connect to Management API ({e}). RabbitMQ Management plugin might be disabled.")

    # 2. Set permissions for user via PUT /api/permissions/{vhost}/{user}
    perm_url = f"{base_url}/permissions/{vhost}/{username}"
    payload = json.dumps({"configure": ".*", "write": ".*", "read": ".*"}).encode('utf-8')
    try:
        req = urllib.request.Request(perm_url, data=payload, method='PUT')
        req.add_header('Content-Type', 'application/json')
        with opener.open(req) as response:
             if response.status in (201, 204):
                print(f"  ✓ Permissions set for user '{username}' on '{vhost}'.")
    except Exception as e:
        print(f"  ⚠ Warning: Could not set permissions ({e})")


def main():
    """Main entry point."""
    rabbitmq_url = os.environ.get(
        'RABBITMQ_URL',
        'amqp://guest:guest@localhost:5672/edi'
    )
    
    print("=" * 60)
    print("EDI Topology Initialization")
    print("=" * 60)
    
    # Ensure vhost exists before connecting via AMQP
    asyncio.run(ensure_vhost_exists(rabbitmq_url))

    print("""
Routing Key Format: {source}.{target}.{domain}.{type}.{action}.{version}

Examples:
  - erp_central.wh_lviv.logistics.despatch_advice.created.v1
  - acc_kyiv.all.finance.invoice.updated.v1
  - *.all.masterdata.item.updated.*  (broadcast to all)
""")
    
    asyncio.run(init_topology(rabbitmq_url))


if __name__ == "__main__":
    main()
