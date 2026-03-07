"""Application entry point — wires up dependencies and runs a demo."""

from mock_codebase.models import Product, Customer
from mock_codebase.repositories import (
    ProductRepository,
    CustomerRepository,
    OrderRepository,
)
from mock_codebase.services import (
    InventoryService,
    PaymentProcessor,
    EmailNotificationService,
    LoyaltyDiscount,
    OrderService,
)


class Application:
    """Top-level application container — composes the full object graph."""

    def __init__(self):
        # Repositories
        self.product_repo = ProductRepository()
        self.customer_repo = CustomerRepository()
        self.order_repo = OrderRepository()

        # Services
        self.inventory = InventoryService(self.product_repo)
        self.payment = PaymentProcessor()
        self.notifications = EmailNotificationService()
        self.discount = LoyaltyDiscount()

        self.order_service = OrderService(
            order_repo=self.order_repo,
            customer_repo=self.customer_repo,
            inventory_service=self.inventory,
            payment_processor=self.payment,
            notification_service=self.notifications,
            discount_strategy=self.discount,
        )

    def seed_data(self) -> None:
        """Populate with sample data."""
        products = [
            Product("P1", "Laptop", 999.99, 10, "electronics"),
            Product("P2", "Headphones", 79.99, 50, "electronics"),
            Product("P3", "Python Book", 39.99, 100, "books"),
        ]
        for p in products:
            self.product_repo.save(p)

        customers = [
            Customer("C1", "Alice", "alice@example.com", "123 Main St", 600),
            Customer("C2", "Bob", "bob@example.com", "456 Oak Ave", 50),
        ]
        for c in customers:
            self.customer_repo.save(c)

    def run(self) -> None:
        self.seed_data()

        order = self.order_service.create_order(
            customer_id="C1",
            items=[
                {"product_id": "P1", "quantity": 1},
                {"product_id": "P2", "quantity": 2},
            ],
        )
        print(f"Created order: {order.to_dict()}")

        self.order_service.cancel_order(order.id)
        print(f"Cancelled order: {order.to_dict()}")


if __name__ == "__main__":
    app = Application()
    app.run()

