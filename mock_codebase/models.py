"""Base model and domain entities."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from enum import Enum


class Entity(ABC):
    """Abstract base class for all domain entities."""

    @abstractmethod
    def validate(self) -> bool:
        ...

    @abstractmethod
    def to_dict(self) -> dict:
        ...


class OrderStatus(Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


@dataclass
class Product(Entity):
    id: str
    name: str
    price: float
    stock: int
    category: str

    def validate(self) -> bool:
        return self.price > 0 and self.stock >= 0 and len(self.name) > 0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "price": self.price,
            "stock": self.stock,
            "category": self.category,
        }

    def reduce_stock(self, quantity: int) -> None:
        if quantity > self.stock:
            raise ValueError(f"Insufficient stock for {self.name}")
        self.stock -= quantity

    def restock(self, quantity: int) -> None:
        self.stock += quantity


@dataclass
class Customer(Entity):
    id: str
    name: str
    email: str
    address: str
    loyalty_points: int = 0

    def validate(self) -> bool:
        return "@" in self.email and len(self.name) > 0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "email": self.email,
            "address": self.address,
            "loyalty_points": self.loyalty_points,
        }

    def add_loyalty_points(self, points: int) -> None:
        self.loyalty_points += points


@dataclass
class OrderItem:
    product: Product
    quantity: int

    @property
    def subtotal(self) -> float:
        return self.product.price * self.quantity


@dataclass
class Order(Entity):
    id: str
    customer: Customer
    items: list[OrderItem] = field(default_factory=list)
    status: OrderStatus = OrderStatus.PENDING
    created_at: datetime = field(default_factory=datetime.now)
    discount_applied: Optional[float] = None

    def validate(self) -> bool:
        return len(self.items) > 0 and self.customer is not None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "customer_id": self.customer.id,
            "items": [
                {"product_id": i.product.id, "quantity": i.quantity}
                for i in self.items
            ],
            "status": self.status.value,
            "total": self.total,
        }

    @property
    def total(self) -> float:
        subtotal = sum(item.subtotal for item in self.items)
        if self.discount_applied:
            subtotal *= 1 - self.discount_applied
        return round(subtotal, 2)

    def add_item(self, product: Product, quantity: int) -> None:
        self.items.append(OrderItem(product=product, quantity=quantity))

    def cancel(self) -> None:
        if self.status in (OrderStatus.SHIPPED, OrderStatus.DELIVERED):
            raise ValueError("Cannot cancel a shipped or delivered order")
        self.status = OrderStatus.CANCELLED

