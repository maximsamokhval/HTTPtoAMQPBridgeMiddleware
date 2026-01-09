# RabbitMQ 4.0 with essential plugins for EDI integration
FROM rabbitmq:4-management

# Enable essential plugins (available in standard distribution)
RUN rabbitmq-plugins enable --offline \
    rabbitmq_shovel \
    rabbitmq_shovel_management \
    rabbitmq_consistent_hash_exchange

# Copy configuration and definitions
COPY rabbitmq.conf /etc/rabbitmq/rabbitmq.conf
COPY definitions.json /etc/rabbitmq/definitions.json

# Fix permissions to ensure rabbitmq user can read them
RUN chown rabbitmq:rabbitmq /etc/rabbitmq/rabbitmq.conf /etc/rabbitmq/definitions.json && \
    chmod 644 /etc/rabbitmq/rabbitmq.conf /etc/rabbitmq/definitions.json