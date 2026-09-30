// Prisma CLI configuration. Prisma is used only for the schema and migrations;
// the Python engine talks to the database through SQLAlchemy (see app/db/models.py).
import "dotenv/config";
import { defineConfig, env } from "prisma/config";

export default defineConfig({
  schema: "prisma/schema.prisma",
  migrations: {
    path: "prisma/migrations",
  },
  datasource: {
    url: env("DATABASE_URL"),
  },
});
