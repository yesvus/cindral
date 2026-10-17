"use client";

import { useActionState } from "react";
import { AdminLoginScreen } from "@/components/admin";
import { signIn } from "./actions";

export function LoginForm() {
  const [error, formAction, pending] = useActionState(signIn, undefined);

  return (
    <AdminLoginScreen
      brandLabel="Cindral"
      homeHref="/"
      busy={pending}
      errorMessage={error}
      onSubmit={({ email, password }) => {
        const form = new FormData();
        form.set("email", email);
        form.set("password", password);
        formAction(form);
      }}
    />
  );
}
