"""Services module — business logic layer."""

from models import User, Order, OrderError


class UserService:
    """Handles user operations."""

    def __init__(self):
        self.users: dict[str, User] = {}

    def create_user(self, name: str, email: str) -> User:
        user = User(name, email)
        self.users[email] = user
        return user

    def get_user(self, email: str) -> User:
        return self.users.get(email)

    def promote_to_admin(self, email: str) -> bool:
        user = self.get_user(email)
        if user:
            user.role = "admin"
            return True
        return False


class OrderService:
    """Handles order creation and management."""

    def __init__(self, user_service: UserService):
        self.user_service = user_service
        self.orders: list[Order] = []

    def create_order(self, user_email: str) -> Order:
        user = self.user_service.get_user(user_email)
        if not user:
            raise OrderError(f"User not found: {user_email}")
        order = Order(user)
        self.orders.append(order)
        return order

    def get_orders_for_user(self, email: str) -> list[Order]:
        return [o for o in self.orders if o.user.email == email]


class NotificationService:
    """Sends notifications to users."""

    def notify(self, user: User, message: str) -> None:
        print(f"Notifying {user.get_display_name()}: {message}")

    def notify_order_placed(self, order: Order) -> None:
        self.notify(order.user, order.summary())

