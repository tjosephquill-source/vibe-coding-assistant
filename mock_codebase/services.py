"""Service layer — business logic orchestration."""

from abc import ABC, abstractmethod
from typing import Protocol

from mock_codebase.models import (
    Product,
    Customer,
    Order,
    OrderItem,
    OrderStatus,
)
from mock_codebase.repositories import (
    ProductRepository,
    CustomerRepository,
    OrderRepository,
)


class NotificationService(Protocol):
    """Protocol for notification delivery (dependency inversion)."""

    def send(self, recipient: str, subject: str, body: str) -> bool:
        ...


class EmailNotificationService:
    """Concrete notification service using email."""

    def send(self, recipient: str, subject: str, body: str) -> bool:
        print(f"[EMAIL] To: {recipient} | Subject: {subject}")
        return True


class SMSNotificationService:
    """Concrete notification service using SMS."""

    def send(self, recipient: str, subject: str, body: str) -> bool:
        print(f"[SMS] To: {recipient} | Message: {body[:160]}")
        return True


class DiscountStrategy(ABC):
    """Abstract discount strategy (Strategy pattern)."""

    @abstractmethod
    def calculate_discount(self, order: Order) -> float:
        ...


class PercentageDiscount(DiscountStrategy):
    def __init__(self, percentage: float):
        self.percentage = percentage

    def calculate_discount(self, order: Order) -> float:
        return self.percentage


class LoyaltyDiscount(DiscountStrategy):
    """Discount based on customer loyalty points."""

    def calculate_discount(self, order: Order) -> float:
        points = order.customer.loyalty_points
        if points > 1000:
            return 0.15
        elif points > 500:
            return 0.10
        elif points > 100:
            return 0.05
        return 0.0


class InventoryService:
    """Manages product stock levels."""

    def __init__(self, product_repo: ProductRepository):
        self.product_repo = product_repo

    def check_availability(self, product_id: str, quantity: int) -> bool:
        product = self.product_repo.find_by_id(product_id)
        if product is None:
            return False
        return product.stock >= quantity

    def reserve_stock(self, product_id: str, quantity: int) -> None:
        product = self.product_repo.find_by_id(product_id)
        if product is None:
            raise ValueError(f"Product {product_id} not found")
        product.reduce_stock(quantity)
        self.product_repo.save(product)

    def release_stock(self, product_id: str, quantity: int) -> None:
        product = self.product_repo.find_by_id(product_id)
        if product is None:
            raise ValueError(f"Product {product_id} not found")
        product.restock(quantity)
        self.product_repo.save(product)


class PaymentProcessor:
    """Handles payment processing."""

    def process_payment(self, order: Order, payment_method: str) -> bool:
        print(
            f"[PAYMENT] Processing ${order.total} via {payment_method} "
            f"for order {order.id}"
        )
        # Simulated payment — always succeeds
        return True

    def refund(self, order: Order) -> bool:
        print(f"[PAYMENT] Refunding ${order.total} for order {order.id}")
        return True


class OrderService:
    """Orchestrates order creation, payment, and notification."""

    def __init__(
        self,
        order_repo: OrderRepository,
        customer_repo: CustomerRepository,
        inventory_service: InventoryService,
        payment_processor: PaymentProcessor,
        notification_service: NotificationService,
        discount_strategy: DiscountStrategy | None = None,
    ):
        self.order_repo = order_repo
        self.customer_repo = customer_repo
        self.inventory = inventory_service
        self.payment = payment_processor
        self.notifications = notification_service
        self.discount_strategy = discount_strategy

    def create_order(
        self,
        customer_id: str,
        items: list[dict],
        payment_method: str = "credit_card",
    ) -> Order:
        customer = self.customer_repo.find_by_id(customer_id)
        if customer is None:
            raise ValueError(f"Customer {customer_id} not found")

        # Check stock for all items
        for item in items:
            if not self.inventory.check_availability(
                item["product_id"], item["quantity"]
            ):
                raise ValueError(
                    f"Product {item['product_id']} insufficient stock"
                )

        # Build order
        order = Order(
            id=f"ORD-{customer_id}-{len(items)}",
            customer=customer,
        )

        for item in items:
            product = self.inventory.product_repo.find_by_id(item["product_id"])
            order.add_item(product, item["quantity"])

        # Apply discount
        if self.discount_strategy:
            discount = self.discount_strategy.calculate_discount(order)
            order.discount_applied = discount

        # Process payment
        if not self.payment.process_payment(order, payment_method):
            raise RuntimeError("Payment failed")

        # Reserve stock
        for item in items:
            self.inventory.reserve_stock(item["product_id"], item["quantity"])

        # Save and confirm
        order.status = OrderStatus.CONFIRMED
        self.order_repo.save(order)

        # Award loyalty points
        customer.add_loyalty_points(int(order.total // 10))
        self.customer_repo.save(customer)

        # Notify
        self.notifications.send(
            recipient=customer.email,
            subject=f"Order {order.id} confirmed",
            body=f"Your order of ${order.total} has been confirmed.",
        )

        return order

    def cancel_order(self, order_id: str) -> None:
        order = self.order_repo.find_by_id(order_id)
        if order is None:
            raise ValueError(f"Order {order_id} not found")

        order.cancel()

        # Release stock
        for item in order.items:
            self.inventory.release_stock(item.product.id, item.quantity)

        # Refund
        self.payment.refund(order)

        self.order_repo.save(order)

        # Notify
        self.notifications.send(
            recipient=order.customer.email,
            subject=f"Order {order_id} cancelled",
            body="Your order has been cancelled and refunded.",
        )

