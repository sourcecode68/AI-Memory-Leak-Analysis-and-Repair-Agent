#include <iostream>

struct Point {
    int x;
    int y;
};

int main() {
    Point* p = new Point{3, 4};
    std::cout << "point: " << p->x << "," << p->y << "\n";
    return 0;
}
