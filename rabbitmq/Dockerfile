# RabbitMQ with essential plugins for EDI integration
FROM rabbitmq:3-management

# Enable essential plugins (available in standard distribution)
RUN rabbitmq-plugins enable --offline \
    rabbitmq_shovel \
    rabbitmq_shovel_management \
    rabbitmq_consistent_hash_exchange
